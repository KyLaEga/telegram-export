"""Screen 3 — Duplicate review (Review Screen).

Data source — the preview dict from `EngineController.scan_done` (a wrapper over
`export_media.scan_preview`). We visualise the losers array (`preview["review"]`):
files that dedup / quality resolution were going to skip as worse versions.

The user ticks the ones to download after all; on "Apply selection and start
download" their `msg_id`s are gathered into a set and sent to MainWindow →
`Options.force_include` of the real `run_export`.

MVC binding: the model is filled STRICTLY in the main GUI thread. Data from the
background scan arrives via the `scan_done(dict)` Qt signal, and inside the screen
is additionally decoupled via `rows_ready(list)`. IMPORTANT: do NOT call
model.blockSignals(True) while filling — it mutes rowsInserted/dataChanged and the
view stays visually empty; use the _bulk_update flag (gating only the counter).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableView,
    QWidget,
)

from .. import theme
from ..i18n import t
from ._fmt import human_size, media_duration


class ReviewScreen(QWidget):
    confirmed = Signal(set)    # set[int] msg_id → force_include for run_export
    back = Signal()
    # Array of skipped_duplicates dicts → fill the model in the MAIN thread.
    rows_ready = Signal(list)

    COL_CHECK, COL_MSG, COL_NAME, COL_SIZE, COL_DUR, COL_REASON = range(6)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Screen safety floor (anti-collapse): props up minimumSizeHint of the
        # QStackedWidget so the review table and button row don't drift off-screen.
        self.setMinimumSize(760, 480)
        self._bulk_update = False   # bulk fill: don't recompute the counter per item
        self._need_files = 0        # from preview: how many already in the plan
        self._summary_mode = "idle"  # idle | scanning | error | loaded
        self._summary_count = 0
        self._summary_error = ""
        self._build()
        self.rows_ready.connect(self._fill_rows)

    def _headers(self) -> list:
        return [t("col_check"), t("col_msg_id"), t("col_name"),
                t("col_size"), t("col_duration"), t("col_reason")]

    # ── layout ──────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = theme.page_layout(self)
        self.title = theme.title_label(t("review_title"))
        root.addWidget(self.title)

        self.summary = theme.hint_label("")
        root.addWidget(self.summary)

        # ── losers table ──
        self.model = QStandardItemModel(0, len(self._headers()), self)
        self.model.setHorizontalHeaderLabels(self._headers())
        self.model.itemChanged.connect(self._on_item_changed)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)  # only checkboxes
        self.table.setShowGrid(True)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        hdr = self.table.horizontalHeader()
        hdr.setHighlightSections(False)
        hdr.setSectionResizeMode(self.COL_NAME, QHeaderView.Stretch)
        for col in (self.COL_CHECK, self.COL_MSG, self.COL_SIZE,
                    self.COL_DUR, self.COL_REASON):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(self._toggle_row)
        root.addWidget(self.table, 1)

        # ── one bottom line: bulk ops (left) + counter + actions ──
        bar = QHBoxLayout()
        bar.setSpacing(theme.SPACING)
        self.back_btn = QPushButton()
        self.back_btn.clicked.connect(self.back.emit)
        self.all_btn = QPushButton()
        self.all_btn.clicked.connect(lambda: self._set_all(True))
        self.none_btn = QPushButton()
        self.none_btn.clicked.connect(lambda: self._set_all(False))

        self.selected_lbl = QLabel()
        self.selected_lbl.setStyleSheet(
            f"font-size: 14px; font-weight: 700; color: {theme.PRIMARY};")

        self.run_btn = QPushButton()
        self.run_btn.setProperty("primary", True)
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self._on_run)

        bar.addWidget(self.back_btn)
        bar.addWidget(self.all_btn)
        bar.addWidget(self.none_btn)
        bar.addStretch(1)
        bar.addWidget(self.selected_lbl)
        bar.addSpacing(theme.SPACING)
        bar.addWidget(self.run_btn)
        root.addLayout(bar)

        self.retranslate()

    def retranslate(self) -> None:
        self.title.setText(t("review_title"))
        self.model.setHorizontalHeaderLabels(self._headers())
        self.back_btn.setText(t("nav_back"))
        self.all_btn.setText(t("btn_select_all"))
        self.none_btn.setText(t("btn_select_none"))
        self.run_btn.setText(t("btn_apply_run"))
        self._render_summary()
        self._update_count()

    def _render_summary(self) -> None:
        if self._summary_mode == "scanning":
            self.summary.setText(t("review_scanning"))
        elif self._summary_mode == "error":
            self.summary.setText(t("review_error", msg=self._summary_error))
        elif self._summary_mode == "loaded":
            text = t("review_summary", need=self._need_files, n=self._summary_count)
            if not self._summary_count:
                text += t("review_summary_none")
            self.summary.setText(text)
        else:
            self.summary.setText("")

    # ── fill from preview-dict ─────────────────────────────────────────────────
    def populate(self, scan_result: dict) -> None:
        """scan_result — the `scan_done` payload: {"preview": {... "review": [...]}}.

        Only extracts the skipped_duplicates array and emits rows_ready(list):
        the actual model fill (_fill_rows) runs in the main GUI thread."""
        preview = (scan_result or {}).get("preview", {}) or {}
        self._need_files = int(preview.get("need_files", 0))
        self.rows_ready.emit(list(preview.get("review", []) or []))

    def _fill_rows(self, losers: list) -> None:
        """rows_ready slot — MAIN thread. No blockSignals on the model: the view
        must receive rowsInserted/dataChanged or the table is visually empty."""
        self._bulk_update = True
        try:
            self.model.removeRows(0, self.model.rowCount())
            for rec in losers:
                chk = QStandardItem()
                chk.setCheckable(True)
                chk.setCheckState(Qt.Unchecked)
                chk.setData(int(rec["msg_id"]), Qt.UserRole)  # source-of-truth msg_id
                chk.setTextAlignment(Qt.AlignCenter)
                self.model.appendRow([
                    chk,
                    self._cell(str(rec.get("msg_id", ""))),
                    self._cell(rec.get("name", "")),
                    self._cell(human_size(rec.get("size", 0))),
                    self._cell(media_duration(rec.get("duration", 0))),
                    self._cell(self._reason(rec)),
                ])
        finally:
            self._bulk_update = False

        self._summary_mode = "loaded"
        self._summary_count = len(losers)
        self._render_summary()
        self._update_count()

    @staticmethod
    def _reason(rec: dict) -> str:
        reason = rec.get("reason", "")
        if reason == "worse-version":
            return t("reason_worse", id=rec.get("master_msg_id", "?"))
        return reason or t("reason_dup")

    @staticmethod
    def _cell(text: str) -> QStandardItem:
        it = QStandardItem(text)
        it.setEditable(False)
        return it

    # ── interaction ─────────────────────────────────────────────────────────────
    def _toggle_row(self, index) -> None:
        item = self.model.item(index.row(), self.COL_CHECK)
        if item is not None:
            item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked
                               else Qt.Checked)

    def _set_all(self, checked: bool) -> None:
        # No blockSignals: dataChanged must reach the view or the checkmarks won't
        # repaint. _bulk_update only defers the counter recompute to the end.
        state = Qt.Checked if checked else Qt.Unchecked
        self._bulk_update = True
        try:
            for row in range(self.model.rowCount()):
                self.model.item(row, self.COL_CHECK).setCheckState(state)
        finally:
            self._bulk_update = False
        self._update_count()

    def _on_item_changed(self, _item) -> None:
        if not self._bulk_update:
            self._update_count()

    def _collect(self) -> "set[int]":
        out: "set[int]" = set()
        for row in range(self.model.rowCount()):
            item = self.model.item(row, self.COL_CHECK)
            if item.checkState() == Qt.Checked:
                out.add(int(item.data(Qt.UserRole)))
        return out

    def _update_count(self) -> None:
        self.selected_lbl.setText(t("lbl_selected", n=len(self._collect())))

    def _on_run(self) -> None:
        self.confirmed.emit(self._collect())

    def set_busy(self, busy: bool) -> None:
        self.run_btn.setEnabled(not busy)
        self.back_btn.setEnabled(not busy)
        self.all_btn.setEnabled(not busy)
        self.none_btn.setEnabled(not busy)

    def show_scanning(self) -> None:
        """Awaiting the scan_preview result (before scan_done arrives)."""
        self.model.removeRows(0, self.model.rowCount())
        self._summary_mode = "scanning"
        self._render_summary()
        self._update_count()

    def show_error(self, message: str) -> None:
        self._summary_mode = "error"
        self._summary_error = message
        self._render_summary()
