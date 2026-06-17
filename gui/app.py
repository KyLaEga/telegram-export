"""GUI entry point: QApplication + a header + a QStackedWidget wizard.

Phase 1: app skeleton, the Qt↔asyncio bridge (EngineController) and the login screen.
Phase 2: settings → duplicate review → download dashboard → upload panel → reports.
Phase 3 (this revision): TensorMedia theme (dark default + light toggle) and full
EN/RU localisation, both switchable live from the header bar.

MainWindow is the coordinator: it owns cfg and the controller, switches screens and
binds each screen's semantic signals to engine jobs. On a theme/language switch it
re-styles custom widgets and retranslates every screen.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

# Ensure the project root (export_media.py / uploader.py) is importable whether the
# GUI is launched via `python -m gui` or run_gui.py from any folder.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QByteArray, QSettings  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QStackedWidget, QVBoxLayout, QWidget,
)

from . import theme                             # noqa: E402
from .controller import EngineController        # noqa: E402
from .i18n import t, translator                 # noqa: E402
from .topbar import TopBar                      # noqa: E402
from .screens.dashboard import DownloadDashboard  # noqa: E402
from .screens.login import LoginScreen          # noqa: E402
from .screens.options import OptionsScreen      # noqa: E402
from .screens.reports import ReportsScreen      # noqa: E402
from .screens.review import ReviewScreen        # noqa: E402
from .screens.upload import UploadScreen        # noqa: E402


class MainWindow(QWidget):
    def __init__(self, controller: EngineController) -> None:
        super().__init__()
        # ── GEOMETRY MANDATE ─────────────────────────────────────────────────
        # A firm "human" default plus a safety floor below which the window
        # physically cannot shrink. Without an explicit minimum the stack collapsed
        # content into an accordion and hid buttons below the screen edge.
        self.setMinimumSize(850, 700)
        self.resize(880, 740)
        self._ctl = controller
        self._cfg: dict = {}
        self._base_options = None   # Options collected on the settings screen
        self._settings = QSettings("TelegramExport", "GUI")

        # ── header + screen stack ──
        self.topbar = TopBar()
        self.stack = QStackedWidget()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.topbar)
        root.addWidget(self.stack, 1)

        # ── screens ──
        self.login = LoginScreen(controller)
        self.options = OptionsScreen(controller)
        self.review = ReviewScreen()
        self.dashboard = DownloadDashboard(controller)
        self.upload = UploadScreen(controller)
        self.reports = ReportsScreen()
        self._screens = (self.login, self.options, self.review, self.dashboard,
                         self.upload, self.reports)
        for w in self._screens:
            self.stack.addWidget(w)

        self._wire()
        self.stack.setCurrentWidget(self.login)
        self._retranslate()
        self._restore_geometry()

    # ── wiring navigation and jobs ──────────────────────────────────────────────
    def _wire(self) -> None:
        self.login.done.connect(self._on_logged_in)

        self.options.request_export.connect(self._on_request_export)
        self.options.request_upload.connect(lambda: self.stack.setCurrentWidget(self.upload))
        self.options.back.connect(lambda: self.stack.setCurrentWidget(self.login))

        self.review.confirmed.connect(self._on_review_confirmed)
        self.review.back.connect(lambda: self.stack.setCurrentWidget(self.options))

        self.dashboard.back.connect(lambda: self.stack.setCurrentWidget(self.options))
        self.upload.back.connect(lambda: self.stack.setCurrentWidget(self.options))

        # When a run finishes we show the persistent reports screen with the totals.
        self.dashboard.finished.connect(self._on_export_finished)
        self.reports.back.connect(lambda: self.stack.setCurrentWidget(self.options))
        self.reports.new_export.connect(lambda: self.stack.setCurrentWidget(self.options))

        # The preview-scan result populates the review screen.
        self._ctl.scan_done.connect(self.review.populate)
        # Review buttons are disabled while the engine is busy scanning.
        self._ctl.busy_changed.connect(self.review.set_busy)
        # A scan failure is shown right on the review screen when it is active.
        self._ctl.failed.connect(self._on_failed)

        # Header switchers.
        self.topbar.theme_changed.connect(self._on_theme_changed)
        translator.language_changed.connect(self._retranslate)

    # ── theme / language ────────────────────────────────────────────────────────
    def _on_theme_changed(self, dark: bool) -> None:
        app = QApplication.instance()
        theme.set_theme(app, dark)
        theme.refresh_custom(self)
        self._settings.setValue("dark_theme", dark)

    def _retranslate(self) -> None:
        self.setWindowTitle(t("app_title"))
        self.topbar.retranslate()
        for s in self._screens:
            fn = getattr(s, "retranslate", None)
            if callable(fn):
                fn()

    def _on_logged_in(self, cfg: dict) -> None:
        self._cfg = cfg
        self.upload.set_cfg(cfg)
        self.stack.setCurrentWidget(self.options)

    def _on_request_export(self, options, want_review: bool) -> None:
        self._base_options = options
        if want_review:
            # "Download everything": preview-scan first, then review skipped duplicates.
            self.review.show_scanning()
            self.stack.setCurrentWidget(self.review)
            self._ctl.scan(self._cfg, options)
        else:
            self._start_download(options)

    def _on_review_confirmed(self, force_ids: set) -> None:
        # msg_ids brought back on review are protected from dedup/quality resolution.
        opts = replace(self._base_options or options_default(),
                       force_include=frozenset(force_ids))
        self._start_download(opts)

    def _start_download(self, options) -> None:
        self.dashboard.reset()
        self.stack.setCurrentWidget(self.dashboard)
        if not self._ctl.start_export(self._cfg, options):
            self.dashboard._append(t("up_busy"))

    def _on_export_finished(self, result: dict) -> None:
        # The dashboard finished a run — populate and show the reports screen (only if
        # the user is still on the dashboard, so we don't yank them off another screen).
        if self.stack.currentWidget() is not self.dashboard:
            return
        self.reports.populate(result or {})
        self.stack.setCurrentWidget(self.reports)

    def _on_failed(self, message: str) -> None:
        if self.stack.currentWidget() is self.review:
            self.review.show_error(message)

    # ── window geometry persistence (QSettings) ─────────────────────────────────
    def _restore_geometry(self) -> None:
        geo = self._settings.value("geometry")
        if isinstance(geo, QByteArray) and not geo.isEmpty():
            self.restoreGeometry(geo)
            # Guard against a previously saved deformed geometry: if a past session
            # wrote a collapsed size, bump it to a safe default and re-centre.
            if (self.width() < self.minimumWidth()
                    or self.height() < self.minimumHeight()):
                self.resize(880, 740)
                self._center_on_screen()
        else:
            self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - self.width()) // 2,
                  (screen.height() - self.height()) // 2)

    def closeEvent(self, event) -> None:
        self._settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)


def options_default():
    import export_media as em
    return em.Options()


def main() -> int:
    # Packaged apps (.app / .msi / .AppImage) launch with a READ-ONLY working
    # directory (often "/"). Pyrogram writes `unknown_errors.txt` to the CWD on any
    # Telegram error it can't map — that write fails with errno 30 and propagates OUT
    # of the error constructor, MASKING the real error and aborting login. Move to a
    # guaranteed-writable directory before anything touches the network.
    import export_media as em
    try:
        os.chdir(em.DATA_DIR)
    except OSError:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("Telegram Export")
    settings = QSettings("TelegramExport", "GUI")
    dark = settings.value("dark_theme", True, type=bool)
    theme.apply_theme(app, dark)
    controller = EngineController()
    win = MainWindow(controller)
    # Cleanly stop the background asyncio loop on exit.
    app.aboutToQuit.connect(controller.shutdown)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
