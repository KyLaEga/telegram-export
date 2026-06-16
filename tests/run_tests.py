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
