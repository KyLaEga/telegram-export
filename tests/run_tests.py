#!/usr/bin/env python3.12
"""Лёгкий регрессионный набор (без pytest) — ловит классы багов, которые уже всплывали.

Запуск:  QT_QPA_PLATFORM=offscreen venv/bin/python3.12 tests/run_tests.py

Покрывает:
  1. Контракт Reporter — каждый публичный метод базового em.Reporter реализован и в
     CliReporter, и в QtReporter (иначе worker падает AttributeError'ом, как было с
     file_skipped в Phase 2.6).
  2. Инверсия чекбоксов options.build_options() — галка = «включить» (Phase 2.7). Защищает
     от случайного возврата `not` и от рассинхрона дефолтов.
  3. «Скачать всё» принудительно включает и блокирует дедуп/качество.
  4. ReportsScreen.populate() — карточки итогов + список реально существующих отчётов.
  5. Форматтеры _fmt (elide_middle, human_size).
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}")
        _FAILURES.append(msg)


# ── 1. Контракт Reporter ────────────────────────────────────────────────────────
def test_reporter_contract() -> None:
    print("test_reporter_contract")
    import export_media as em
    from gui.reporter import QtReporter

    base_methods = {
        name for name, _ in inspect.getmembers(em.Reporter, inspect.isfunction)
        if not name.startswith("_")
    }
    for cls in (em.CliReporter, QtReporter):
        for name in sorted(base_methods):
            check(callable(getattr(cls, name, None)),
                  f"{cls.__name__} реализует .{name}()")


# ── 2/3. Инверсия чекбоксов + блокировка «Скачать всё» ──────────────────────────
def test_options_build(app) -> None:
    print("test_options_build")
    from gui.controller import EngineController
    from gui.screens.options import OptionsScreen

    ctl = EngineController()
    try:
        scr = OptionsScreen(ctl)

        # Дефолты: дедуп/качество ВКЛ, видео обрабатывается (faststart on), fast on.
        o = scr.build_options()
        check(o.use_dedup is True, "дефолт: use_dedup=True (галка дедупа стоит)")
        check(o.use_quality is True, "дефолт: use_quality=True (галка качества стоит)")
        check(o.faststart is True, "дефолт: faststart=True (видео обрабатывается)")

        # Галка «Сохранять видео без обработки» → faststart выключается.
        scr.cb_keep_raw_video.setChecked(True)
        check(scr.build_options().faststart is False,
              "keep_raw_video=✓ → faststart=False")

        # Снять дедуп/качество → флаги падают в False (прямое соответствие, без not).
        scr.cb_dedup.setChecked(False)
        scr.cb_quality.setChecked(False)
        o2 = scr.build_options()
        check(o2.use_dedup is False, "dedup=✗ → use_dedup=False")
        check(o2.use_quality is False, "quality=✗ → use_quality=False")

        # «Скачать всё» — принудительно включает и блокирует дедуп/качество.
        scr.cb_download_all.setChecked(True)
        check(scr.cb_dedup.isChecked() and not scr.cb_dedup.isEnabled(),
              "download_all=✓ → дедуп включён и заблокирован")
        check(scr.cb_quality.isChecked() and not scr.cb_quality.isEnabled(),
              "download_all=✓ → качество включено и заблокировано")
        o3 = scr.build_options()
        check(o3.use_dedup and o3.use_quality,
              "download_all=✓ → build_options даёт dedup+quality True")

        # Спинбоксы получили минимальную высоту (анти-клиппинг).
        check(scr.sp_workers.minimumHeight() >= 30 and
              scr.sp_connections.minimumHeight() >= 30,
              "спинбоксы: minimumHeight ≥ 30")

        # Ссылки/комиксы: дефолт выкл, формат-комбо заблокировано до включения.
        od = scr.build_options()
        check(od.download_links is False and od.link_format == "cbz",
              "дефолт: download_links=False, link_format=cbz")
        check(not scr.cmb_link_format.isEnabled(),
              "формат-комбо выключено, пока ссылки не включены")
        scr.cb_download_links.setChecked(True)
        scr.cmb_link_format.setCurrentIndex(1)        # PDF
        ol = scr.build_options()
        check(ol.download_links is True and ol.link_format == "pdf",
              "ссылки=✓ + PDF → build_options даёт download_links=True, link_format=pdf")
        check(scr.cmb_link_format.isEnabled(), "ссылки=✓ → формат-комбо разблокировано")
    finally:
        ctl.shutdown()


# ── 4. ReportsScreen ────────────────────────────────────────────────────────────
def test_reports_screen(app) -> None:
    print("test_reports_screen")
    from PySide6.QtWidgets import QPushButton

    from gui.screens.reports import ReportsScreen

    with tempfile.TemporaryDirectory() as tmp:
        # Кладём два реальных отчёта и один отсутствующий — экран покажет только существующие.
        with open(os.path.join(tmp, "export_log.txt"), "w") as fh:
            fh.write("лог\n")
        with open(os.path.join(tmp, "quality_report.txt"), "w") as fh:
            fh.write("качество\n")

        scr = ReportsScreen()
        scr.populate({
            "dest": tmp,
            "stopped": False,
            "stats": {"downloaded": 5, "skipped": 2, "dedup": 1,
                      "repaired": 0, "failed": 3, "bytes_done": 1024 ** 3},
        })
        check(scr.card_done._value.text() == "5", "карточка «Скачано» = 5")
        check(scr.card_failed._value.text() == "3", "карточка «Ошибки» = 3")
        check(scr.reports_hint.isHidden(), "hint скрыт — отчёты найдены")
        open_btns = scr.reports_box.findChildren(QPushButton)
        check(len(open_btns) == 2, f"ровно 2 кнопки «Открыть» (найдено {len(open_btns)})")
        check(scr.open_folder_btn.isEnabled(), "«Открыть папку» активна (dest существует)")

        # Пустой dest → кнопки папки гаснут, hint виден.
        scr.populate({"dest": "/nonexistent/xyz", "stats": {}, "stopped": True})
        check(not scr.open_folder_btn.isEnabled(),
              "несуществующий dest → «Открыть папку» заблокирована")
        check(not scr.reports_hint.isHidden(), "нет отчётов → hint снова виден")


# ── 5. Форматтеры (локализованные единицы) ──────────────────────────────────────
def test_fmt() -> None:
    print("test_fmt")
    from gui.i18n import translator
    from gui.screens._fmt import elide_middle, human_size

    check(elide_middle("short.mp4", 64) == "short.mp4", "короткое имя не трогаем")
    long = "a" * 100 + ".mp4"
    e = elide_middle(long, 40)
    check(len(e) <= 40 and "…" in e, "длинное имя усечено по середине с …")

    translator.set_language("en")
    check(human_size(0) == "0 B", "human_size(0)=EN")
    check(human_size(1536).endswith("KB"), "human_size KB (EN)")
    check("GB" in human_size(2 * 1024 ** 3), "human_size GB (EN)")

    translator.set_language("ru")
    check(human_size(0) == "0 Б", "human_size(0)=RU")
    check("ГБ" in human_size(2 * 1024 ** 3), "human_size ГБ (RU)")
    translator.set_language("en")


# ── 6. i18n: паритет ключей EN/RU + переключение языка ──────────────────────────
def test_i18n() -> None:
    print("test_i18n")
    from gui.i18n import _EN, _RU, t, translator

    check(set(_EN) == set(_RU),
          f"EN/RU ключи совпадают (EN={len(_EN)}, RU={len(_RU)})")
    # Нет пустых значений (каждый ключ переведён в обоих языках).
    empties = [k for d in (_EN, _RU) for k, v in d.items() if not str(v).strip()]
    check(not empties, f"нет пустых переводов (пустых: {len(empties)})")

    translator.set_language("en")
    en = t("btn_run_export")
    translator.set_language("ru")
    ru = t("btn_run_export")
    check(en == "Start export" and ru == "Запустить экспорт",
          "переключение языка меняет строку")
    # Форматирование с подстановкой не падает и подставляет значения.
    check("4" in t("warn_conn", total=4, workers=1, per=4), "tr(**fmt) подставляет")
    # Неизвестный ключ деградирует в сам ключ (без исключения).
    check(t("___missing___") == "___missing___", "неизвестный ключ → сам ключ")

    # Raw '&' в тексте Qt трактуется как мнемоника (ломает QGroupBox/QPushButton/
    # QCheckBox: «Threads & speed» → «Threads _speed»). Запрещаем сырой амперсанд.
    amp = [k for d in (_EN, _RU) for k, v in d.items() if "&" in str(v)]
    check(not amp, f"нет сырых '&' в строках (мнемоника Qt): {amp}")

    # fmt_speed локализован (а не RU-движковый).
    from gui.screens._fmt import fmt_speed
    translator.set_language("en")
    check(fmt_speed(5 * 1024 * 1024).endswith("MB/s"), "fmt_speed EN → MB/s")
    translator.set_language("ru")
    check(fmt_speed(5 * 1024 * 1024).endswith("МБ/с"), "fmt_speed RU → МБ/с")
    translator.set_language("en")


# ── 7. Локализация экранов: сборка + retranslate в обоих языках без падений ──────
def test_localization_smoke(app) -> None:
    print("test_localization_smoke")
    from gui.controller import EngineController
    from gui.i18n import translator
    from gui.app import MainWindow

    ctl = EngineController()
    try:
        win = MainWindow(ctl)
        translator.set_language("ru")
        ru_run = win.options.run_btn.text()
        translator.set_language("en")
        en_run = win.options.run_btn.text()
        check(ru_run == "Запустить экспорт" and en_run == "Start export",
              "MainWindow retranslate переключает все экраны")
        check(win.dashboard.card_done._cap.text() == "✅ Downloaded",
              "StatCard caption локализован (EN)")
    finally:
        ctl.shutdown()


# ── 8. Тема: dark/light переключение + refresh кастомных виджетов ────────────────
def test_theme_toggle(app) -> None:
    print("test_theme_toggle")
    import gui.theme as theme

    theme.set_theme(app, True)
    check(theme.BG == "#1E1F22" and theme.is_dark(), "dark: BG=#1E1F22")
    theme.set_theme(app, False)
    check(theme.BG == "#F2F3F5" and not theme.is_dark(), "light: BG=#F2F3F5")
    # refresh_custom не падает на StatCard/WarningBanner и перекрашивает их.
    card = theme.StatCard("X", theme.SUCCESS)
    banner = theme.WarningBanner()
    banner.set_message("hi", warn=True)
    theme.set_theme(app, True)
    theme.refresh_custom(card)
    theme.refresh_custom(banner)
    check("1E1F22" in card.styleSheet() or theme.SURFACE in card.styleSheet(),
          "StatCard перекрашен под активную тему")


# ── 9. Writable data dir (read-only bundle → errno 30 fix) ──────────────────────
def test_data_dir() -> None:
    print("test_data_dir")
    import export_media as em

    check(em._is_writable_dir(tempfile.gettempdir()), "_is_writable_dir(tmp)=True")
    check(not em._is_writable_dir("/no_write_here_probe_dir"),
          "_is_writable_dir(read-only /)=False")
    check(em._user_data_dir().endswith("Telegram Export"),
          "_user_data_dir ends with app name")
    # The resolved DATA_DIR must always be writable (this is what the .session,
    # config.json and Pyrogram's unknown_errors.txt are written to).
    check(em._is_writable_dir(em.DATA_DIR), "DATA_DIR is writable")
    check(em.CONFIG_PATH.startswith(em.DATA_DIR), "CONFIG_PATH lives under DATA_DIR")


# ── 9. Скачивание по ссылкам (link_export): чистые функции + сборка ───────────────
def test_link_export() -> None:
    print("test_link_export")
    import zipfile

    import link_export as le

    # extract_links: web_page + entity-url + голые URL, без дублей, http-only, порядок.
    class _Ent:
        def __init__(self, url): self.url = url

    class _WP:
        url = "https://s.tld/c/1"; display_url = "s.tld/c/1"; title = "Comic 1"

    class _Msg:
        web_page = _WP()
        text = "x https://i.tld/a.jpg https://i.tld/a.jpg"
        entities = [_Ent("https://h.tld/f.cbz")]
        caption = None; caption_entities = []

    check(le.extract_links(_Msg()) ==
          ["https://s.tld/c/1", "https://h.tld/f.cbz", "https://i.tld/a.jpg"],
          "extract_links: web_page+entity+text, dedup, http-only, порядок")

    check(le.classify_url("https://x/y.PDF?a=1") == "file", "classify_url .pdf → file")
    check(le.classify_url("https://x/y.webp") == "image", "classify_url .webp → image")
    check(le.classify_url("https://x/read/1") == "page", "classify_url без ext → page")

    # scrape_image_urls: <img>/lazy/<a>, относительные → абсолютные, мусор отброшен.
    html = ('<img src="p1.jpg"><img data-src="/a/p2.png">'
            '<a href="p3.webp">x</a><img src="/logo.png">')
    check(le.scrape_image_urls(html, "https://c.tld/r/") ==
          ["https://c.tld/r/p1.jpg", "https://c.tld/a/p2.png", "https://c.tld/r/p3.webp"],
          "scrape: разрешает относительные и режет logo")
    check(le.scrape_image_urls('<meta property="og:image" content="/o.jpg">',
                               "https://c.tld/") == ["https://c.tld/o.jpg"],
          "scrape: og:image как фолбэк")

    check(le.slugify("Hi/There #2!") == "HiThere_2", "slugify санитайзит имя")
    jpg_hdr = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
    check(le.sniff_image_ext(jpg_hdr) == ".jpg", "sniff_image_ext JPEG")
    check(le.sniff_image_ext(b"\x89PNG\r\n\x1a\nXX") == ".png", "sniff_image_ext PNG")

    d = tempfile.mkdtemp()
    # Assembly is now PATH-based (низкая память): кладём страницы на диск и собираем.
    p_jpg = os.path.join(d, "a.jpg"); open(p_jpg, "wb").write(jpg_hdr)
    p_png = os.path.join(d, "b.png"); open(p_png, "wb").write(b"\x89PNG\r\n\x1a\nXX")
    cbz = os.path.join(d, "c.cbz")
    le.save_cbz([p_jpg, p_png], cbz)
    with zipfile.ZipFile(cbz) as z:
        check(z.namelist() == ["0001.jpg", "0002.png"],
              "save_cbz: страницы пронумерованы (сборка с диска)")

    # PDF из реального JPEG на диске (JPEG встраивается без Pillow; Pillow есть только в env).
    try:
        import io as _io

        from PIL import Image
        b = _io.BytesIO(); Image.new("RGB", (12, 9), (10, 20, 30)).save(b, "JPEG")
        real = b.getvalue()
        check(le._jpeg_dimensions(real) == (12, 9, 3), "_jpeg_dimensions из SOF")
        rp = os.path.join(d, "real.jpg"); open(rp, "wb").write(real)
        pdf = os.path.join(d, "df.pdf")
        le.save_pdf([rp], pdf)
        raw = open(pdf, "rb").read()
        check(raw[:5] == b"%PDF-" and raw.rstrip().endswith(b"%%EOF"),
              "потоковый PDF: корректные заголовок и хвост")
    except ImportError:
        pass

    # process_links_sync: страница-галерея → один комикс; повтор — пропуск; прямой файл.
    # http(...) отдаёт HTML-страницы (байты); download(url,dst) пишет файл на диск.
    page = '<img src="p1.jpg"><img src="p2.jpg">'
    html_store = {
        "https://c.tld/read/77": (page.encode(), "text/html; charset=utf-8",
                                  "https://c.tld/read/77"),
    }
    bin_store = {
        "https://c.tld/read/p1.jpg": jpg_hdr,
        "https://c.tld/read/p2.jpg": jpg_hdr,
        "https://h.tld/f.cbz": b"PK\x03\x04zipdata",
    }

    def fake_http(url, referer=None, timeout=0, max_bytes=0):
        return html_store[url]

    def fake_dl(url, dst, referer=None, timeout=0, max_bytes=0):
        with open(dst, "wb") as f:
            f.write(bin_store[url])
        return "application/octet-stream", url

    r1 = le.process_links_sync(77, ["https://c.tld/read/77"], d, "cbz",
                               label="t", http=fake_http, downloader=fake_dl, workers=2)
    check(r1["saved"] == 1, "process_links_sync: страница → 1 комикс собран (параллельно, с диска)")
    r2 = le.process_links_sync(77, ["https://c.tld/read/77"], d, "cbz",
                               label="t", http=fake_http, downloader=fake_dl, workers=2)
    check(r2["skipped"] == 1 and r2["saved"] == 0,
          "process_links_sync: повторный прогон пропускает готовое (идемпотентность)")
    r3 = le.process_links_sync(9, ["https://h.tld/f.cbz"], d, "cbz",
                               http=fake_http, downloader=fake_dl)
    check(r3["saved"] == 1, "process_links_sync: прямой .cbz сохранён как есть")
    check(os.path.exists(os.path.join(d, "msg_9_f.cbz")), "прямой файл получил msg_-имя")
    # Временные папки сборки комикса убираются за собой.
    check(not [x for x in os.listdir(d) if x.startswith(".comic_")],
          "временные папки сборки удалены")


def main() -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    test_reporter_contract()
    test_options_build(app)
    test_reports_screen(app)
    test_fmt()
    test_i18n()
    test_localization_smoke(app)
    test_theme_toggle(app)
    test_data_dir()
    test_link_export()

    print()
    if _FAILURES:
        print(f"❌ ПРОВАЛено проверок: {len(_FAILURES)}")
        for m in _FAILURES:
            print(f"   - {m}")
        return 1
    print("✅ Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
