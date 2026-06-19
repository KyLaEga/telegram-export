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
import os
import re
import shutil
import ssl
import struct
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
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

# Защита от страниц-бомб и контроль памяти.
MAX_BYTES = 2 * 1024 * 1024 * 1024       # 2 ГБ потолок на ОДИН файл (качаем на диск чанками)
MAX_HTML = 25 * 1024 * 1024              # HTML-страницу читаем в память, но не больше 25 МБ
CHUNK = 256 * 1024                        # размер чанка потоковой загрузки (постоянная память)
HTTP_TIMEOUT = 40
# Сколько страниц комикса качать ПАРАЛЛЕЛЬНО (главный ускоритель: вместо 50
# последовательных запросов — пул потоков). На лимитах сервера не агрессивно.
LINK_IMG_WORKERS = 6


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
@lru_cache(maxsize=1)
def _ssl_context() -> "ssl.SSLContext":
    """SSL-контекст с доверенными корнями. В упакованном приложении (Briefcase) у
    встроенного Python НЕТ системных CA-сертификатов, и любой https-запрос падает с
    CERTIFICATE_VERIFY_FAILED. certifi даёт переносимый набор корней; без него —
    стандартный контекст (работает при запуске из исходников)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                    # noqa: BLE001 — нет certifi → системные корни
        return ssl.create_default_context()


def _open(url: str, referer: str | None, timeout: int):
    """Открыть HTTP(S)-поток с нашим User-Agent (иначе часть сайтов отдаёт 403) и без
    gzip (Accept-Encoding: identity — чтобы не распаковывать в памяти)."""
    headers = {"User-Agent": _UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = referer
    return urlopen(Request(url, headers=headers), timeout=timeout,  # noqa: S310 — схема проверена
                   context=_ssl_context())


def http_get(url: str, referer: str | None = None, timeout: int = HTTP_TIMEOUT,
             max_bytes: int = MAX_HTML) -> tuple[bytes, str, str]:
    """GET HTML-страницы → (тело, content_type, итоговый_url). Тело целиком в память, но с
    жёстким потолком max_bytes (страницы небольшие). Крупные файлы — через stream_to_file."""
    with _open(url, referer, timeout) as r:
        data = r.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"ответ больше {max_bytes // (1024 * 1024)} МБ — пропуск")
        return data, (r.headers.get_content_type() or "").lower(), r.geturl()


def stream_to_file(url: str, dst: str, referer: str | None = None,
                   timeout: int = HTTP_TIMEOUT, max_bytes: int = MAX_BYTES) -> tuple[str, str]:
    """Качает URL НА ДИСК чанками (постоянная память ~CHUNK, а не файл целиком в RAM).
    Возвращает (content_type, итоговый_url). Это ключ к низкому потреблению памяти."""
    written = 0
    with _open(url, referer, timeout) as r:
        ctype = (r.headers.get_content_type() or "").lower()
        final = r.geturl()
        with open(dst, "wb") as f:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"файл больше {max_bytes // (1024 * 1024)} МБ — пропуск")
                f.write(chunk)
    return ctype, final


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


def _sniff_file(path: str) -> str | None:
    """Расширение картинки по первым байтам ФАЙЛА на диске (без чтения целиком)."""
    try:
        with open(path, "rb") as f:
            return sniff_image_ext(f.read(16))
    except OSError:
        return None


# ── 6. Сборка картинок (с ДИСКА) в CBZ / PDF — низкое потребление памяти ─────────
def _page_arcname(i: int, path: str) -> str:
    ext = _sniff_file(path) or (_ext_of(path) if _ext_of(path) in IMAGE_EXTS else ".jpg")
    return f"{i:04d}{ext}"


def save_cbz(paths: list[str], out_path: str) -> None:
    """CBZ = ZIP со страницами 0001.ext, 0002.ext… Каждая картинка СТРИМИТСЯ с диска
    (zip.write), а не держится в памяти — любой формат, без сторонних библиотек."""
    if not paths:
        raise ValueError("нет картинок для CBZ")
    tmp = out_path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for i, p in enumerate(paths, 1):
            z.write(p, _page_arcname(i, p))
    os.replace(tmp, out_path)


def save_pdf(paths: list[str], out_path: str) -> None:
    """Все страницы (файлы на диске) в один PDF, по одной странице за раз (память ≈ одна
    страница). JPEG встраивается без потерь (/DCTDecode); прочие форматы Pillow приводит
    к JPEG на диске. Без Pillow умеет только JPEG-страницы (для остального — формат CBZ)."""
    if not paths:
        raise ValueError("нет картинок для PDF")
    tmpdir = tempfile.mkdtemp(prefix="pdfjpg_")
    try:
        metas = _jpeg_pages_meta(_ensure_jpegs(paths, tmpdir))
        if not metas:
            raise RuntimeError(
                "нет пригодных JPEG-страниц для PDF. Для PNG/WebP и т.п. нужен Pillow "
                "(pip install Pillow) — либо выберите формат CBZ.")
        tmp = out_path + ".part"
        _write_pdf_from_jpegs(metas, tmp)
        os.replace(tmp, out_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _ensure_jpegs(paths: list[str], tmpdir: str) -> list[str]:
    """Каждую страницу приводим к JPEG НА ДИСКЕ (по одной за раз): JPEG берём как есть
    (без потерь), прочее — через Pillow в RGB-JPEG. Без Pillow не-JPEG отбрасываются."""
    Image = None
    try:
        from PIL import Image as _Image
        Image = _Image
    except ImportError:
        pass
    out: list[str] = []
    for i, p in enumerate(paths, 1):
        if _sniff_file(p) == ".jpg":
            out.append(p)
            continue
        if Image is None:
            continue
        try:
            im = Image.open(p)
            if im.mode not in ("RGB", "L", "CMYK"):      # PDF не хранит альфу/палитру
                im = im.convert("RGB")
            jp = os.path.join(tmpdir, f"{i:04d}.jpg")
            im.save(jp, "JPEG", quality=92)
            im.close()
            out.append(jp)
        except Exception:                                # noqa: BLE001 — битую страницу пропускаем
            continue
    return out


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


def _jpeg_pages_meta(paths: list[str]) -> list[tuple]:
    """Для каждой JPEG-страницы вернуть (path, w, h, comps). Не-JPEG/битые отбрасываются.
    Файлы читаются по одному (для разбора SOF), в памяти максимум одна страница."""
    metas = []
    for p in paths:
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if sniff_image_ext(data) != ".jpg":
            continue
        dim = _jpeg_dimensions(data)
        if dim:
            metas.append((p, *dim))
    return metas


def _write_pdf_from_jpegs(metas: list[tuple], out_path: str) -> None:
    """Пишет PDF ПОТОКОВО прямо в файл: каждый JPEG встраивается как есть (/DCTDecode,
    без потерь), читаясь с диска по одной странице. Память ≈ одна страница, а не весь том.

    Раскладка объектов: на страницу 3 (image+content+page), затем Pages и Catalog."""
    n = len(metas)
    pages_obj_num = n * 3 + 1
    catalog_num = n * 3 + 2
    offsets: list[int] = [0] * (catalog_num + 1)         # 1-based; [0] не используется
    cs = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}
    kids: list[int] = []

    with open(out_path, "wb") as f:
        pos = 0

        def w(b: bytes) -> None:
            nonlocal pos
            f.write(b)
            pos += len(b)

        w(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        obj = 0
        for path, wid, hei, comps in metas:
            with open(path, "rb") as g:
                data = g.read()
            obj += 1
            offsets[obj] = pos
            w(b"%d 0 obj\n<< /Type /XObject /Subtype /Image /Width %d /Height %d "
              b"/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\n"
              b"stream\n" % (obj, wid, hei, cs.get(comps, b"/DeviceRGB"), len(data)))
            w(data)
            w(b"\nendstream\nendobj\n")
            img_num = obj

            content = b"q\n%d 0 0 %d 0 0 cm\n/Im0 Do\nQ\n" % (wid, hei)
            obj += 1
            offsets[obj] = pos
            w(b"%d 0 obj\n<< /Length %d >>\nstream\n" % (obj, len(content)))
            w(content)
            w(b"\nendstream\nendobj\n")
            content_num = obj

            obj += 1
            offsets[obj] = pos
            w(b"%d 0 obj\n<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
              b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>\nendobj\n"
              % (obj, pages_obj_num, wid, hei, img_num, content_num))
            kids.append(obj)

        offsets[pages_obj_num] = pos
        w(b"%d 0 obj\n<< /Type /Pages /Kids [%s] /Count %d >>\nendobj\n"
          % (pages_obj_num, b" ".join(b"%d 0 R" % k for k in kids), len(kids)))
        offsets[catalog_num] = pos
        w(b"%d 0 obj\n<< /Type /Catalog /Pages %d 0 R >>\nendobj\n"
          % (catalog_num, pages_obj_num))

        xref_pos = pos
        w(b"xref\n0 %d\n" % (catalog_num + 1))
        w(b"0000000000 65535 f \n")
        for k in range(1, catalog_num + 1):
            w(b"%010d 00000 n \n" % offsets[k])
        w(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
          % (catalog_num + 1, catalog_num, xref_pos))


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
                       label: str = "", http=http_get, downloader=stream_to_file,
                       workers: int = LINK_IMG_WORKERS, log=lambda *_a: None) -> dict:
    """Скачивает/собирает всё по ссылкам одного сообщения. Идемпотентно: если целевой
    файл уже существует — пропускает (возобновляемость без БД). Картинки страницы качаются
    НА ДИСК и ПАРАЛЛЕЛЬНО. Возвращает {'saved','skipped','failed'}. http/downloader/log
    внедряются для тестов."""
    fmt = fmt if fmt in FORMATS else "cbz"
    res = {"saved": 0, "skipped": 0, "failed": 0}
    loose_imgs: list[str] = []                           # прямые картинки-ссылки → один комикс
    first_url = urls[0] if urls else ""

    for url in urls:
        kind = classify_url(url)
        try:
            if kind == "file":
                _save_direct_file(msg_id, url, out_dir, downloader, log, res)
            elif kind == "image":
                loose_imgs.append(url)
            else:  # page
                _save_page_comic(msg_id, url, out_dir, fmt, http, downloader, workers, log, res)
        except Exception as e:                            # noqa: BLE001 — одна ссылка не валит остальные
            res["failed"] += 1
            log(f"link msg {msg_id}: {url} → ошибка: {e}")

    if loose_imgs:
        try:
            _build_comic(msg_id, loose_imgs, out_dir, fmt,
                         label or _filename_from_url(first_url), None,
                         downloader, workers, res, log)
        except Exception as e:                            # noqa: BLE001
            res["failed"] += 1
            log(f"link msg {msg_id}: сборка из картинок → ошибка: {e}")
    return res


def _save_direct_file(msg_id, url, out_dir, downloader, log, res) -> None:
    name = _filename_from_url(url)
    ext = _ext_of(url) or ".bin"
    dst = out_path(out_dir, msg_id, os.path.splitext(name)[0], ext)
    if os.path.exists(dst):
        res["skipped"] += 1
        return
    tmp = dst + ".part"
    try:
        downloader(url, tmp)                             # стрим на диск чанками
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    res["saved"] += 1
    log(f"link msg {msg_id}: файл сохранён {os.path.basename(dst)}")


def _save_page_comic(msg_id, url, out_dir, fmt, http, downloader, workers, log, res) -> None:
    dst = out_path(out_dir, msg_id, _page_label(url), "." + fmt)
    if os.path.exists(dst):
        res["skipped"] += 1
        return
    html, ctype, final_url = http(url)
    # Сама ссылка отдала картинку/файл (редирект) — это не HTML-страница.
    if "html" not in ctype and not ctype.startswith("text"):
        ext = sniff_image_ext(html)
        target = out_path(out_dir, msg_id, _page_label(url), ext or _ext_of(final_url) or ".bin")
        if os.path.exists(target):
            res["skipped"] += 1
            return
        _write_bytes(target, html)                       # одиночная картинка/файл — как есть
        res["saved"] += 1
        return
    img_urls = scrape_image_urls(_decode_html(html, ctype), final_url)
    if not img_urls:
        res["skipped"] += 1
        log(f"link msg {msg_id}: на странице {url} картинок не найдено")
        return
    _build_comic(msg_id, img_urls, out_dir, fmt, _page_label(url), final_url,
                 downloader, workers, res, log)


def _build_comic(msg_id, img_urls, out_dir, fmt, label, referer,
                 downloader, workers, res, log) -> None:
    """Качает все страницы параллельно НА ДИСК (во временную папку) и собирает в один
    файл выбранного формата. Память не зависит от числа/размера страниц."""
    dst = out_path(out_dir, msg_id, label, "." + fmt)
    if os.path.exists(dst):
        res["skipped"] += 1
        return
    tmpdir = tempfile.mkdtemp(prefix=".comic_", dir=out_dir)
    try:
        paths = _download_images(img_urls, tmpdir, referer, downloader, workers, log)
        if not paths:
            res["failed"] += 1
            return
        # Одиночная картинка — это не комикс: кладём файлом-картинкой (кроме явного PDF).
        if len(paths) == 1 and fmt != "pdf":
            single = out_path(out_dir, msg_id, label, _sniff_file(paths[0]) or ".jpg")
            if os.path.exists(single):
                res["skipped"] += 1
                return
            os.replace(paths[0], single)
            res["saved"] += 1
            return
        (save_pdf if fmt == "pdf" else save_cbz)(paths, dst)
        res["saved"] += 1
        log(f"link msg {msg_id}: комикс собран {os.path.basename(dst)} ({len(paths)} стр.)")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _download_images(img_urls, tmpdir, referer, downloader, workers, log) -> list[str]:
    """Параллельно качает картинки в tmpdir как 0001.ext, 0002.ext… (порядок страниц
    сохраняется). Не-картинки и сбойные ссылки отбрасываются. Возвращает пути по порядку."""
    def fetch(item):
        i, url = item
        part = os.path.join(tmpdir, f"{i:04d}.part")
        try:
            downloader(url, part, referer=referer)
        except Exception as e:                            # noqa: BLE001
            log(f"  страница {url} → {e}")
            return None
        ext = _sniff_file(part)
        if ext is None:                                   # сервер отдал не картинку
            try:
                os.remove(part)
            except OSError:
                pass
            return None
        final = os.path.join(tmpdir, f"{i:04d}{ext}")
        os.replace(part, final)
        return (i, final)

    got: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for r in ex.map(fetch, list(enumerate(img_urls, 1))):
            if r is not None:
                got.append(r)
    got.sort(key=lambda t: t[0])
    return [p for _i, p in got]


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


LINK_STATE_FILE = ".link_state.json"


def _load_link_state(out_dir: str) -> tuple[int, set]:
    """Возвращает (last_id, failed_ids). last_id — водяной знак: до него история уже
    обработана, поэтому повторные прогоны читают только НОВЫЕ сообщения."""
    import json
    try:
        with open(os.path.join(out_dir, LINK_STATE_FILE), encoding="utf-8") as f:
            d = json.load(f)
        return int(d.get("last_id", 0)), set(d.get("failed", []))
    except (OSError, ValueError):
        return 0, set()


def _save_link_state(out_dir: str, last_id: int, failed: set) -> None:
    import json
    tmp = os.path.join(out_dir, LINK_STATE_FILE + ".part")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_id": int(last_id), "failed": sorted(failed)}, f)
        os.replace(tmp, os.path.join(out_dir, LINK_STATE_FILE))
    except OSError:
        pass


# ── 9. Фаза скачивания по ссылкам (зовётся движком из run_export) ──────────────
async def run_link_phase(app, chat_id, dest, fmt, reporter, stop_event,
                         log=lambda *_a: None, t=None) -> dict:
    """Качает комиксы/файлы по ссылкам в dest/comics_and_links. ИНКРЕМЕНТАЛЬНО: помнит
    водяной знак (последний обработанный msg_id) и при повторном запуске читает только
    новые сообщения + повторяет ранее сбойные. Сеть уносится в поток (asyncio.to_thread),
    чтобы не блокировать event-loop. t — переводчик статусов (язык интерфейса)."""
    from pyrogram.errors import FloodWait

    try:
        from export_media import get_messages_retry
    except Exception:                                    # noqa: BLE001 — fallback без FloodWait-ретрая
        async def get_messages_retry(a, c, ids):
            r = await a.get_messages(c, ids)
            return r if isinstance(r, list) else [r]
    if t is None:                                        # автономный вызов/тесты — берём из движка
        try:
            from export_media import _t as t
        except Exception:                                # noqa: BLE001
            t = lambda k, **kw: k                         # noqa: E731 — крайний фолбэк
    out_dir = os.path.join(dest, LINK_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    fmt = fmt if fmt in FORMATS else "cbz"
    totals = {"messages": 0, "saved": 0, "skipped": 0, "failed": 0}
    last_id, failed = _load_link_state(out_dir)

    reporter.status(t("link_start", fmt=fmt.upper(), dir=LINK_SUBDIR))
    if last_id:
        reporter.status(t("link_scan_inc", since=last_id))

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    async def handle(m) -> None:
        urls = extract_links(m)
        if not urls:
            return
        totals["messages"] += 1
        res = await asyncio.to_thread(
            lambda mid=m.id, u=urls, lab=_message_label(m):
            process_links_sync(mid, u, out_dir, fmt, label=lab, log=log))
        for k in ("saved", "skipped", "failed"):
            totals[k] += res[k]
        (failed.add if res["failed"] else failed.discard)(m.id)
        if totals["saved"] and totals["saved"] % 5 == 0:
            reporter.status_inline(t("link_progress", saved=totals["saved"],
                                     skip=totals["skipped"], fail=totals["failed"]))

    # ── новые сообщения (id > last_id), новейшие → старые, ранний выход у водяного знака ──
    max_seen, completed, offset_id = last_id, False, 0
    while not stopped() and not completed:
        got = 0
        try:
            async for m in app.get_chat_history(chat_id, offset_id=offset_id):
                offset_id = m.id
                got += 1
                if m.id <= last_id:
                    completed = True
                    break
                if stopped():
                    break
                max_seen = max(max_seen, m.id)
                await handle(m)
        except FloodWait as e:
            w = int(getattr(e, "value", 0) or 0)
            reporter.status(t("link_flood", w=w))
            await asyncio.sleep(w + 1)
            continue
        if got == 0:
            completed = True

    # ── повтор ранее сбойных сообщений (по id; skip-if-exists делает успех дешёвым) ──
    if failed and not stopped():
        for chunk in (sorted(failed)[i:i + 190] for i in range(0, len(failed), 190)):
            if stopped():
                break
            try:
                msgs = await get_messages_retry(app, chat_id, chunk)
            except Exception:                            # noqa: BLE001
                continue
            for m in msgs:
                if m is None or getattr(m, "empty", False):
                    failed.discard(getattr(m, "id", 0))
                    continue
                if stopped():
                    break
                await handle(m)

    # Водяной знак двигаем только если скан НЕ прервали — иначе пропустили бы хвост.
    _save_link_state(out_dir, max_seen if (completed and not stopped()) else last_id, failed)

    reporter.status_clear()
    reporter.status(t("link_done", saved=totals["saved"], skip=totals["skipped"],
                      fail=totals["failed"], msgs=totals["messages"]))
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
