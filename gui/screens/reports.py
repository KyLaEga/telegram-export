"""Screen 6 — Reports and run summary.

Shown after a download finishes (signal `DownloadDashboard.finished(result)`).
Condenses the run result into a persistent view (not the dashboard's scrolling log):
  • summary cards from result["stats"] (downloaded/skipped/dedup/repaired/errors);
  • the destination path with "Open folder" / "Show in Finder" buttons;
  • the list of text reports in dest (export_log.txt, quality_report.txt,
    verify_report.txt, error_export.log) — only the ones that actually exist, each
    opened by the system app with one button.

The engine is not touched here — the screen is purely presentational; navigation
("← To settings", "🔄 New export") is handled by MainWindow via signals.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
)

from .. import theme
from ..i18n import t
from ._fmt import elide_middle, human_size, open_path, reveal_in_finder

# Report names the engine may leave in the destination folder. The keys are i18n
# keys for the title/description; kept in sync with export_media report names.
_REPORT_FILES = (
    ("export_log.txt", "report_export_title", "report_export_desc"),
    ("quality_report.txt", "report_quality_title", "report_quality_desc"),
    ("verify_report.txt", "report_verify_title", "report_verify_desc"),
    ("error_export.log", "report_error_title", "report_error_desc"),
)


class ReportsScreen(QWidget):
    back = Signal()          # ← to settings
    new_export = Signal()    # start another export (to settings, like back)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._dest = ""
        self._last_result: dict = {}
        self._build()

    # ── layout ──────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = theme.page_layout(self)
        self.title = theme.title_label(t("rep_title"))
        root.addWidget(self.title)

        # ── run summary header ──
        self.summary_lbl = QLabel(t("rep_not_done"))
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setStyleSheet("font-weight:600;")
        root.addWidget(self.summary_lbl)

        # ── summary cards ──
        self.cards_box, cards_lay = theme.section(t("sec_dl_summary"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.SPACING)
        grid.setVerticalSpacing(theme.SPACING)
        self.card_done = theme.StatCard(t("card_downloaded"), theme.SUCCESS)
        self.card_skipped = theme.StatCard(t("card_already"), theme.TEAL)
        self.card_dedup = theme.StatCard(t("card_dedup"), theme.INFO)
        self.card_repaired = theme.StatCard(t("card_repaired"), theme.PURPLE)
        self.card_failed = theme.StatCard(t("card_failed"), theme.DANGER)
        self._cards = (self.card_done, self.card_skipped, self.card_dedup,
                       self.card_repaired, self.card_failed)
        for i, card in enumerate(self._cards):
            grid.addWidget(card, i // 3, i % 3)
        cards_lay.addLayout(grid)
        root.addWidget(self.cards_box)

        # ── destination folder ──
        self.dest_box, dest_lay = theme.section(t("sec_dest"))
        self.dest_lbl = QLabel("—")
        self.dest_lbl.setWordWrap(True)
        self.dest_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dest_lbl.setStyleSheet(
            "font-family:'SF Mono','Menlo','Courier New',monospace;font-size:11px;")
        dest_lay.addWidget(self.dest_lbl)
        dest_row = QHBoxLayout()
        dest_row.setSpacing(theme.SPACING)
        self.open_folder_btn = QPushButton()
        self.open_folder_btn.clicked.connect(self._open_folder)
        self.reveal_btn = QPushButton()
        self.reveal_btn.clicked.connect(self._reveal_folder)
        dest_row.addWidget(self.open_folder_btn)
        dest_row.addWidget(self.reveal_btn)
        dest_row.addStretch(1)
        dest_lay.addLayout(dest_row)
        root.addWidget(self.dest_box)

        # ── reports list (filled in populate) ──
        self.reports_box, self.reports_lay = theme.section(t("sec_text_reports"))
        self.reports_hint = QLabel(t("rep_none_short"))
        self.reports_hint.setWordWrap(True)
        self.reports_hint.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        self.reports_lay.addWidget(self.reports_hint)
        root.addWidget(self.reports_box)

        root.addStretch(1)

        # ── actions ──
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACING)
        self.back_btn = QPushButton()
        self.back_btn.clicked.connect(self.back.emit)
        self.new_btn = QPushButton()
        self.new_btn.setProperty("primary", True)
        self.new_btn.clicked.connect(self.new_export.emit)
        actions.addWidget(self.back_btn)
        actions.addStretch(1)
        actions.addWidget(self.new_btn)
        root.addLayout(actions)

        self.retranslate()

    def retranslate(self) -> None:
        self.title.setText(t("rep_title"))
        self.cards_box.setTitle(t("sec_dl_summary"))
        self.card_done.set_caption(t("card_downloaded"))
        self.card_skipped.set_caption(t("card_already"))
        self.card_dedup.set_caption(t("card_dedup"))
        self.card_repaired.set_caption(t("card_repaired"))
        self.card_failed.set_caption(t("card_failed"))
        self.dest_box.setTitle(t("sec_dest"))
        self.open_folder_btn.setText(t("btn_open_folder"))
        self.reveal_btn.setText(t("btn_reveal"))
        self.reports_box.setTitle(t("sec_text_reports"))
        self.back_btn.setText(t("nav_back_params"))
        self.new_btn.setText(t("btn_new_export"))
        # Re-render the dynamic summary + reports list in the new language.
        if self._last_result:
            self.populate(self._last_result)
        else:
            self.summary_lbl.setText(t("rep_not_done"))
            self.reports_hint.setText(t("rep_none_short"))

    # ── fill from the run result ────────────────────────────────────────────────
    def populate(self, result: dict) -> None:
        """Takes the run_export result dict (stats/stopped/dest)."""
        result = result or {}
        self._last_result = result
        stats = result.get("stats") or {}
        stopped = bool(result.get("stopped"))
        self._dest = str(result.get("dest") or "")

        self.card_done.set_value(stats.get("downloaded", 0))
        self.card_skipped.set_value(stats.get("skipped", 0))
        self.card_dedup.set_value(stats.get("dedup", 0))
        self.card_repaired.set_value(stats.get("repaired", 0))
        self.card_failed.set_value(stats.get("failed", 0))

        gb = stats.get("bytes_done", 0) / (1024 ** 3)
        key = "rep_stopped" if stopped else "rep_done"
        self.summary_lbl.setText(t(key, n=stats.get("downloaded", 0), gb=gb,
                                   failed=stats.get("failed", 0)))

        self.dest_lbl.setText(self._dest or "—")
        has_dest = bool(self._dest and os.path.isdir(self._dest))
        self.open_folder_btn.setEnabled(has_dest)
        self.reveal_btn.setEnabled(has_dest)

        self._fill_reports()

    def _fill_reports(self) -> None:
        # Remove previous report rows (keeping the hint).
        while self.reports_lay.count():
            item = self.reports_lay.takeAt(0)
            w = item.widget()
            if w is not None and w is not self.reports_hint:
                w.setParent(None)
                w.deleteLater()

        found = 0
        for fname, title_key, desc_key in _REPORT_FILES:
            path = os.path.join(self._dest, fname) if self._dest else ""
            if not path or not os.path.exists(path):
                continue
            found += 1
            self.reports_lay.addWidget(
                self._report_row(path, fname, t(title_key), t(desc_key)))

        if found:
            self.reports_hint.hide()
        else:
            self.reports_hint.setText(t("rep_none_long"))
            self.reports_hint.show()
            self.reports_lay.addWidget(self.reports_hint)

    def _report_row(self, path: str, fname: str, title: str, desc: str) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACING)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        text = QLabel(f"<b>{title}</b> · {fname} · {human_size(size)}<br>"
                      f"<span style='color:{theme.TEXT_MUTED};font-size:11px;'>{desc}</span>")
        text.setWordWrap(True)
        open_btn = QPushButton(t("btn_open"))
        open_btn.clicked.connect(lambda _=False, p=path: self._open_file(p))
        lay.addWidget(text, 1)
        lay.addWidget(open_btn, 0)
        return row

    # ── filesystem actions ───────────────────────────────────────────────────────
    def _open_folder(self) -> None:
        if not open_path(self._dest):
            self.summary_lbl.setText(t("rep_open_folder_fail"))

    def _reveal_folder(self) -> None:
        if not reveal_in_finder(self._dest):
            self.summary_lbl.setText(t("rep_reveal_fail"))

    def _open_file(self, path: str) -> None:
        if not open_path(path):
            self.summary_lbl.setText(t("rep_open_file_fail", path=elide_middle(path, 60)))
