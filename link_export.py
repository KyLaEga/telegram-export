#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
Скачивание комиксов и других файлов ПО ССЫЛКАМ из сообщений Telegram.

Движок export_media.py качает только медиа, ПРИКРЕПЛЁННОЕ к сообщению (фото, видео,
документ — включая .pdf/.cbz, присланные файлом). Но многие комиксы лежат во ВНЕШНИХ
ссылках: либо web_page-превью, либо просто URL в тексте/подписи. Раньше такие сообщения
движок игнорировал. Этот модуль закрывает пробел:

  • вытаскивает ссылки из сообщения (web_page + текст/подпись и их entities);
  • ПРЯМОЙ файл по ссылке (…/comic.pdf, …/scan.cbz, …/page.jpg, .zip/.cbr/.epub) —
    скачивает как есть;
  • ВЕБ-СТРАНИЦА-галерея — парсит <img>/<a>/og:image, скачивает картинки по порядку и
    СОБИРАЕТ их в ОДИН файл выбранного формата (PDF или CBZ);
  • несколько прямых картинок в одном сообщении — тоже собирает в один комикс.

Зависимости: только стандартная библиотека. PDF из картинок собирается через Pillow,
если он установлен (умеет любые форматы — JPEG/PNG/WebP/GIF); без Pillow есть
встроенный безбиблиотечный сборщик PDF из JPEG (DCTDecode). CBZ — это просто ZIP с
картинками, поэтому работает для ЛЮБОГО формата без сторонних библиотек.

Сетевые/IO-функции — синхронные (urllib); движок зовёт process_links_sync через
asyncio.to_thread, чтобы не блокировать event-loop. Чистые функции (extract_links,
classify_url, scrape_image_urls, slugify, сборка PDF/CBZ) покрыты тестами.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import struct
import zipfile
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

# Папка-приёмник для всего, что пришло по ссылкам (рядом с медиа на SSD).
LINK_SUBDIR = "comics_and_links"

# Поддерживаемые форматы сборки комикса из набора картинок.
FORMATS = ("cbz", "pdf")

# Расширения, которые трактуем как «готовый файл» — качаем как есть, без пересборки.
PACKAGED_EXTS = {
    ".pdf", ".cbz", ".cbr", ".zip", ".rar", ".7z",
    ".epub", ".mobi", ".azw3", ".djvu", ".fb2",
}
# Расширения картинок (страницы комикса) — их собираем в PDF/CBZ.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif"}

# Имена-«мусор» на страницах: иконки/логотипы/спрайты/реклама — не страницы комикса.
_JUNK_IMG_RE = re.compile(
    r"(sprite|favicon|logo|icon|avatar|banner|button|/ads?[/_.]|doubleclick)", re.I)

# Грубый детектор URL в тексте (для сообщений без entities/предпросмотра).
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.I)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 telegram-export")

# Защита от страниц-бомб: не тянем в память бесконечный ответ.
MAX_BYTES = 200 * 1024 * 1024            # 200 МБ на один файл/страницу
HTTP_TIMEOUT = 40


# ── 1. Извлечение ссылок из сообщения ─────────────────────────────────────────
def extract_links(message) -> list[str]:
    """Все внешние ссылки из сообщения, БЕЗ дублей, с сохранением порядка.

    Берём: web_page.url/display_url (ссылка-превью), markdown-ссылки (entity.url у
    TEXT_LINK) и «голые» URL в тексте/подписи. message — объект Pyrogram Message либо
    любой duck-typed аналог (для тестов)."""
    urls: list[str] = []

    wp = getattr(message, "web_page", None)
    if wp is not None:
        for attr in ("url", "display_url"):
            u = getattr(wp, attr, None)
            if u:
                urls.append(u)

    for text_attr, ent_attr in (("text", "entities"), ("caption", "caption_entities")):
        text = getattr(message, text_attr, None)
        # У Pyrogram message.text — это Str с .markdown/.html; приводим к обычной строке.
        text = str(text) if text else ""
        for ent in (getattr(message, ent_attr, None) or []):
            u = getattr(ent, "url", None)             # TEXT_LINK: подпись скрывает URL
            if u:
                urls.append(u)
        if text:
            urls.extend(_URL_RE.findall(text))

    return _dedup_keep_order(_clean(u) for u in urls)


def _clean(url: str) -> str:
    return (url or "").strip().rstrip(".,);]")           # хвостовая пунктуация из текста


def _dedup_keep_order(items) -> list[str]:
    seen, out = set(), []
    for it in items:
        if it and it.startswith(("http://", "https://")) and it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ── 2. Классификация ссылки ───────────────────────────────────────────────────
def _ext_of(url: str) -> str:
    """Расширение из ПУТИ url (без query/fragment), в нижнем регистре, с точкой."""
    path = urlparse(url).path
    return os.path.splitext(path)[1].lower()


def classify_url(url: str) -> str:
    """'file' — готовый файл (pdf/cbz/zip…); 'image' — картинка; 'page' — веб-страница."""
    ext = _ext_of(url)
    if ext in PACKAGED_EXTS:
        return "file"
    if ext in IMAGE_EXTS:
        return "image"
    return "page"


# ── 3. Парсинг картинок со страницы ───────────────────────────────────────────
class _ImageHarvester(HTMLParser):
    """Собирает кандидатов-картинки: <img> (вкл. lazy-атрибуты и srcset), <source
    srcset>, <a href> на картинку и og:image. Порядок появления = порядок страниц."""

    _IMG_ATTRS = ("src", "data-src", "data-original", "data-lazy-src",
                  "data-url", "data-image", "data-full")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.og: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "img":
            for key in self._IMG_ATTRS:
                if d.get(key):
                    self.images.append(d[key])
            if d.get("srcset"):
                self.images.append(_first_srcset(d["srcset"]))
        elif tag == "source" and d.get("srcset"):
            self.images.append(_first_srcset(d["srcset"]))
        elif tag == "a":
            href = d.get("href", "")
            if os.path.splitext(urlparse(href).path)[1].lower() in IMAGE_EXTS:
                self.images.append(href)
        elif tag == "meta":
            prop = (d.get("property") or d.get("name") or "").lower()
            if prop in ("og:image", "og:image:url", "twitter:image") and d.get("content"):
                self.og.append(d["content"])


def _first_srcset(srcset: str) -> str:
    """Первый URL из srcset ('a.jpg 1x, b.jpg 2x' → 'a.jpg')."""
    first = srcset.split(",")[0].strip()
    return first.split()[0] if first else ""


def scrape_image_urls(html: str, base_url: str) -> list[str]:
    """Абсолютные URL картинок со страницы по порядку (без дублей, без явного мусора).

    Сначала пытаемся по <img>/<a>/<source>; если ничего пригодного не нашли — падаем на
    og:image (часто единственная обложка). Относительные ссылки разрешаем через base_url.
    """
    h = _ImageHarvester()
    try:
        h.feed(html)
    except Exception:                                    # noqa: BLE001 — кривой HTML не валит проход
        pass

    def keep(raw: str) -> str | None:
        raw = (raw or "").strip()
        if not raw or raw.startswith("data:"):
            return None
        absu = urljoin(base_url, raw)
        if not absu.startswith(("http://", "https://")):
            return None
        if _JUNK_IMG_RE.search(absu):
            return None
        return absu

    out = _dedup_keep_order(filter(None, (keep(u) for u in h.images)))
    if not out:
        out = _dedup_keep_order(filter(None, (keep(u) for u in h.og)))
    return out


# ── 4. HTTP ───────────────────────────────────────────────────────────────────
def http_get(url: str, referer: str | None = None,
             timeout: int = HTTP_TIMEOUT) -> tuple[bytes, str, str]:
    """GET → (тело, content_type, итоговый_url). Identity-кодировка (без gzip-распаковки),
    свой User-Agent (иначе часть сайтов отдаёт 403). Обрезает тело по MAX_BYTES."""
    headers = {"User-Agent": _UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = referer
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as r:             # noqa: S310 — http(s) проверен выше
        data = r.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError(f"ответ больше {MAX_BYTES // (1024 * 1024)} МБ — пропуск")
        ctype = (r.headers.get_content_type() or "").lower()
        return data, ctype, r.geturl()


# ── 5. Распознавание формата картинки по сигнатуре ────────────────────────────
def sniff_image_ext(data: bytes) -> str | None:
    """Расширение картинки по «магическим» байтам (надёжнее, чем по URL)."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    return None


# ── 6. Сборка картинок в CBZ / PDF ────────────────────────────────────────────
def _page_ext(name: str, data: bytes) -> str:
    return sniff_image_ext(data) or (_ext_of(name) if _ext_of(name) in IMAGE_EXTS else ".jpg")


def save_cbz(images: list[tuple[str, bytes]], out_path: str) -> None:
    """CBZ = ZIP с картинками 0001.ext, 0002.ext… Любой формат, без сторонних библиотек."""
    if not images:
        raise ValueError("нет картинок для CBZ")
    tmp = out_path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for i, (name, data) in enumerate(images, 1):
            z.writestr(f"{i:04d}{_page_ext(name, data)}", data)
    os.replace(tmp, out_path)


def save_pdf(images: list[tuple[str, bytes]], out_path: str) -> None:
    """Все картинки в один PDF (по странице на картинку). Через Pillow (любые форматы),
    а если его нет — безбиблиотечной сборкой из JPEG."""
    if not images:
        raise ValueError("нет картинок для PDF")
    tmp = out_path + ".part"
    if not _save_pdf_pillow(images, tmp):
        _save_pdf_jpeg_only(images, tmp)
    os.replace(tmp, out_path)


def _save_pdf_pillow(images: list[tuple[str, bytes]], out_path: str) -> bool:
    """Сборка PDF через Pillow. False, если Pillow недоступен (тогда вызвать fallback)."""
    try:
        from PIL import Image
    except ImportError:
        return False
    pages = []
    for _name, data in images:
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception:                                # noqa: BLE001 — битую страницу пропускаем
            continue
        if im.mode in ("RGBA", "LA", "P"):               # PDF не хранит альфу — на белый фон
            im = im.convert("RGB")
        elif im.mode not in ("RGB", "L", "CMYK"):
            im = im.convert("RGB")
        pages.append(im)
    if not pages:
        raise ValueError("Pillow не смог открыть ни одной картинки для PDF")
    pages[0].save(out_path, "PDF", save_all=True, append_images=pages[1:])
    return True


def _jpeg_dimensions(data: bytes) -> tuple[int, int, int] | None:
    """(width, height, components) из SOF-маркера JPEG, или None."""
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", data[i + 2:i + 4])[0]
        # SOF0/1/2… (кроме DHT/DAC/SOS-подобных) несут размеры кадра.
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = struct.unpack(">H", data[i + 5:i + 7])[0]
            w = struct.unpack(">H", data[i + 7:i + 9])[0]
            comps = data[i + 9]
            return w, h, comps
        i += 2 + seg
    return None


def _save_pdf_jpeg_only(images: list[tuple[str, bytes]], out_path: str) -> None:
    """Безбиблиотечный PDF: каждый JPEG встраивается как есть (/DCTDecode, без потерь).
    Не-JPEG страницы пропускаются — без Pillow их не сконвертировать (используйте CBZ)."""
    pages = []
    for _name, data in images:
        if sniff_image_ext(data) != ".jpg":
            continue
        dim = _jpeg_dimensions(data)
        if dim:
            pages.append((data, *dim))
    if not pages:
        raise RuntimeError(
            "PDF без Pillow умеет только JPEG-страницы, а их нет. "
            "Установите Pillow (pip install Pillow) или выберите формат CBZ.")

    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)                                 # номер объекта (1-based)

    cs = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}
    kids: list[int] = []
    # На страницу — 3 объекта (image+content+page); объект Pages идёт сразу за ними.
    pages_obj_num = len(pages) * 3 + 1
    for data, w, h, comps in pages:
        img_num = add(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
            b"/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\n"
            b"stream\n" % (w, h, cs.get(comps, "/DeviceRGB").encode(), len(data))
            + data + b"\nendstream")
        content = b"q\n%d 0 0 %d 0 0 cm\n/Im0 Do\nQ\n" % (w, h)
        content_num = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
        page_num = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> "
            b"/Contents %d 0 R >>" % (pages_obj_num, w, h, img_num, content_num))
        kids.append(page_num)

    pages_num = add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids)))
    catalog_num = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_num)

    buf = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for num, body in enumerate(objs, 1):
        offsets.append(len(buf))
        buf += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref_pos = len(buf)
    buf += b"xref\n0 %d\n" % (len(objs) + 1)
    buf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        buf += b"%010d 00000 n \n" % off
    buf += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, catalog_num, xref_pos))
    with open(out_path, "wb") as f:
        f.write(buf)


# ── 7. Имена файлов ───────────────────────────────────────────────────────────
def slugify(text: str, maxlen: int = 60) -> str:
    """Безопасный для ФС фрагмент имени из произвольного текста/URL."""
    text = (text or "").strip()
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"[^\w.\- ]+", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text).strip("._-")
    return text[:maxlen] or ""


def _filename_from_url(url: str) -> str:
    base = os.path.basename(urlparse(url).path)
    return base or urlparse(url).netloc


def out_path(out_dir: str, msg_id: int, label: str, ext: str) -> str:
    slug = slugify(label) or "link"
    return os.path.join(out_dir, f"msg_{msg_id}_{slug}{ext}")


# ── 8. Обработка ссылок одного сообщения (синхронно; зовётся в asyncio.to_thread) ─
def process_links_sync(msg_id: int, urls: list[str], out_dir: str, fmt: str,
                       label: str = "", http=http_get,
                       log=lambda *_a: None) -> dict:
    """Скачивает/собирает всё по ссылкам одного сообщения. Идемпотентно: если целевой
    файл уже существует — пропускает (возобновляемость без БД). Возвращает счётчики
    {'saved','skipped','failed'}. http/log внедряются для тестов."""
    fmt = fmt if fmt in FORMATS else "cbz"
    res = {"saved": 0, "skipped": 0, "failed": 0}
    images: list[tuple[str, bytes]] = []                 # прямые картинки → один комикс
    first_url = urls[0] if urls else ""

    for url in urls:
        kind = classify_url(url)
        try:
            if kind == "file":
                _save_direct_file(msg_id, url, out_dir, http, log, res)
            elif kind == "image":
                data, _ct, _fu = http(url)
                images.append((url, data))
            else:  # page
                _save_page_comic(msg_id, url, out_dir, fmt, http, log, res)
        except Exception as e:                            # noqa: BLE001 — одна ссылка не валит остальные
            res["failed"] += 1
            log(f"link msg {msg_id}: {url} → ошибка: {e}")

    if images:
        _assemble_comic(msg_id, images, out_dir, fmt,
                        label or _filename_from_url(first_url), res, log)
    return res


def _save_direct_file(msg_id, url, out_dir, http, log, res) -> None:
    name = _filename_from_url(url)
    ext = _ext_of(url) or ".bin"
    dst = out_path(out_dir, msg_id, os.path.splitext(name)[0], ext)
    if os.path.exists(dst):
        res["skipped"] += 1
        return
    data, _ct, _fu = http(url)
    tmp = dst + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)
    res["saved"] += 1
    log(f"link msg {msg_id}: файл сохранён {os.path.basename(dst)} ({len(data)} б)")


def _save_page_comic(msg_id, url, out_dir, fmt, http, log, res) -> None:
    dst = out_path(out_dir, msg_id, _page_label(url), "." + fmt)
    if os.path.exists(dst):
        res["skipped"] += 1
        return
    html, ctype, final_url = http(url)
    # Сама ссылка отдала картинку/файл (редирект) — это не HTML-страница.
    if "html" not in ctype and not ctype.startswith("text"):
        ext = sniff_image_ext(html)
        if ext:
            _assemble_comic(msg_id, [(final_url, html)], out_dir, fmt, _page_label(url),
                            res, log)
            return
        # бинарь не-картинка → сохраняем как есть
        _write_bytes(out_path(out_dir, msg_id, _page_label(url), _ext_of(final_url) or ".bin"),
                     html)
        res["saved"] += 1
        return
    img_urls = scrape_image_urls(_decode_html(html, ctype), final_url)
    if not img_urls:
        res["skipped"] += 1
        log(f"link msg {msg_id}: на странице {url} картинок не найдено")
        return
    images: list[tuple[str, bytes]] = []
    for iu in img_urls:
        try:
            data, _ct, _fu = http(iu, referer=final_url)
            if sniff_image_ext(data):
                images.append((iu, data))
        except Exception as e:                            # noqa: BLE001
            log(f"link msg {msg_id}: картинка {iu} → {e}")
    if not images:
        res["failed"] += 1
        return
    _assemble_comic(msg_id, images, out_dir, fmt, _page_label(url), res, log)


def _assemble_comic(msg_id, images, out_dir, fmt, label, res, log) -> None:
    # Одиночная картинка — это не комикс: сохраняем самим файлом-картинкой.
    if len(images) == 1 and fmt != "pdf":
        name, data = images[0]
        ext = sniff_image_ext(data) or ".jpg"
        dst = out_path(out_dir, msg_id, label, ext)
        if os.path.exists(dst):
            res["skipped"] += 1
            return
        _write_bytes(dst, data)
        res["saved"] += 1
        return
    dst = out_path(out_dir, msg_id, label, "." + fmt)
    if os.path.exists(dst):
        res["skipped"] += 1
        return
    (save_pdf if fmt == "pdf" else save_cbz)(images, dst)
    res["saved"] += 1
    log(f"link msg {msg_id}: комикс собран {os.path.basename(dst)} ({len(images)} стр.)")


def _write_bytes(dst: str, data: bytes) -> None:
    tmp = dst + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)


def _page_label(url: str) -> str:
    p = urlparse(url)
    tail = os.path.basename(p.path.rstrip("/")) or p.netloc
    return slugify(tail)


def _decode_html(data: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w\-]+)", ctype)
    enc = m.group(1) if m else "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


# ── 9. Фаза скачивания по ссылкам (зовётся движком из run_export) ──────────────
async def run_link_phase(app, chat_id, dest, fmt, reporter, stop_event,
                         log=lambda *_a: None) -> dict:
    """Проходит историю канала, в каждом сообщении ищет ссылки и качает/собирает их
    в dest/comics_and_links. Сетевые операции уносятся в поток (asyncio.to_thread),
    чтобы не блокировать event-loop pyrogram. Возвращает сводку счётчиков."""
    from pyrogram.errors import FloodWait

    out_dir = os.path.join(dest, LINK_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    fmt = fmt if fmt in FORMATS else "cbz"
    totals = {"messages": 0, "saved": 0, "skipped": 0, "failed": 0}

    reporter.status(f"\n🔗 Скачиваю комиксы и файлы по ссылкам (формат комиксов: "
                    f"{fmt.upper()}) → {LINK_SUBDIR}/")

    offset_id = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        got = 0
        try:
            async for m in app.get_chat_history(chat_id, offset_id=offset_id):
                offset_id = m.id
                got += 1
                if stop_event is not None and stop_event.is_set():
                    break
                urls = extract_links(m)
                if not urls:
                    continue
                totals["messages"] += 1
                label = _message_label(m)
                res = await asyncio.to_thread(
                    process_links_sync, m.id, urls, out_dir, fmt, label,
                    http_get, log)
                for k in ("saved", "skipped", "failed"):
                    totals[k] += res[k]
                if totals["saved"] and totals["saved"] % 5 == 0:
                    reporter.status_inline(
                        f"\r   ссылок обработано: сохранено {totals['saved']}, "
                        f"пропущено {totals['skipped']}, ошибок {totals['failed']}…")
        except FloodWait as e:
            w = int(getattr(e, "value", 0) or 0)
            reporter.status(f"\n⏳ FloodWait при чтении истории: ждём {w} c…")
            await asyncio.sleep(w + 1)
            continue
        if got == 0:
            break

    reporter.status_clear()
    reporter.status(
        f"🔗 Ссылки готовы: новых сохранено {totals['saved']}, уже было "
        f"{totals['skipped']}, ошибок {totals['failed']} "
        f"(сообщений со ссылками: {totals['messages']}).")
    return totals


def _message_label(message) -> str:
    """Короткая подпись для имени файла: из web_page.title, текста или подписи."""
    wp = getattr(message, "web_page", None)
    if wp is not None and getattr(wp, "title", None):
        return slugify(str(wp.title))
    for attr in ("caption", "text"):
        v = getattr(message, attr, None)
        if v:
            return slugify(str(v).splitlines()[0])
    return ""
