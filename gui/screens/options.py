"""Screen 2 — Export settings + the "Download everything" trigger.

Builds `export_media.Options` from the form and decides control flow:
  • "Download everything" OFF → start the download immediately (request_export,
    want_review=False) → dashboard;
  • "Download everything" ON → preview-scan first and the duplicate Review screen
    (request_export, want_review=True), where the user brings losers back into the plan.

The screen never touches the engine directly — it only emits semantic signals;
MainWindow (which owns cfg and the controller) starts jobs and switches screens.

UI: "Configuration" / "Processing options" / "Threads & speed" groups; an interactive
warning banner (workers×connections>6 → orange ⚠️); a spinner that blocks "Start export"
while the engine is busy scanning/starting.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
)

import export_media as em
from .. import theme
from ..i18n import t
from ..controller import EngineController


class OptionsScreen(QWidget):
    # (options, want_review) — MainWindow decides: scan+review or download directly.
    request_export = Signal(object, bool)
    request_upload = Signal()          # go to the upload panel
    back = Signal()                    # back to login

    # Total connections to one DC above which Telegram starts throttling
    # (see memory tg-downloader-tuning: gentle profile — 1 worker × 4 conn = 4 ≤ 6).
    CONN_WARN_THRESHOLD = 6

    def __init__(self, controller: EngineController, parent=None) -> None:
        super().__init__(parent)
        self._ctl = controller
        # Content lives in a scroll area (see _build), so the screen can shrink without
        # compressing the form — the safety floor is much lower than the content height.
        self.setMinimumSize(560, 360)
        self._build()
        self._ctl.busy_changed.connect(self._on_busy)
        self._refresh_warning()

    # ── layout ──────────────────────────────────────────────────────────────
    def _build(self) -> None:
        # Sections scroll; the action bar is pinned outside the scroll area so
        # "Start export" stays visible no matter how short the window gets.
        root, outer = theme.scroll_page(self)
        self.title = theme.title_label(t("opt_title"))
        root.addWidget(self.title)

        # ── Configuration: what and where ("Download everything" toggle) ──
        self.cfg_box, cl = theme.section(t("sec_config"))
        self.cb_download_all = QCheckBox()
        self.cb_download_all.setStyleSheet("font-weight: 600;")
        self.cb_download_all.toggled.connect(self._on_download_all_toggled)
        cl.addWidget(self.cb_download_all)
        self.hint_download_all = theme.hint_label("")
        cl.addWidget(self.hint_download_all)
        root.addWidget(self.cfg_box)

        # ── Processing options ──
        # Affirmative wording. Checkbox semantics are INVERTED vs the CLI flags:
        # ticked = "enable the useful behaviour", so build_options() takes values
        # as-is (not via `not`).
        self.proc_box, pl = theme.section(t("sec_processing"))

        self.cb_keep_raw_video = QCheckBox()       # ticked = skip faststart remux
        self.cb_dedup = QCheckBox()                # ticked = dedup on (default)
        self.cb_dedup.setChecked(True)
        self.cb_quality = QCheckBox()              # ticked = pick best version (default)
        self.cb_quality.setChecked(True)

        for cb in (self.cb_keep_raw_video, self.cb_dedup, self.cb_quality):
            pl.addWidget(cb)
        self.hint_processing = theme.hint_label("")
        pl.addWidget(self.hint_processing)
        root.addWidget(self.proc_box)

        # ── Links & comics: download external links, not just attached media ──
        self.links_box, ll = theme.section(t("sec_links"))
        self.cb_download_links = QCheckBox()
        self.cb_download_links.toggled.connect(self._on_links_toggled)
        ll.addWidget(self.cb_download_links)
        fmt_row = QHBoxLayout()
        fmt_row.setSpacing(theme.SPACING)
        self.lbl_link_format = QLabel()
        self.cmb_link_format = QComboBox()
        # userData carries the engine value; the label is just the display text.
        self.cmb_link_format.addItem("CBZ", "cbz")
        self.cmb_link_format.addItem("PDF", "pdf")
        self.cmb_link_format.setEnabled(False)        # unlocked only when links are on
        fmt_row.addWidget(self.lbl_link_format)
        fmt_row.addWidget(self.cmb_link_format)
        fmt_row.addStretch(1)
        ll.addLayout(fmt_row)
        self.hint_links = theme.hint_label("")
        ll.addWidget(self.hint_links)
        root.addWidget(self.links_box)

        # ── Threads & speed (parallelism) ──
        # No height limits on the group or the form — the container freely grows
        # downward when long labels wrap.
        self.perf_box, perf_lay = theme.section(t("sec_threads"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(theme.SPACING)
        # Guaranteed vertical gap between rows: a label and the next row's spinbox
        # cannot physically overlap even when the caption wraps to 2 lines.
        form.setVerticalSpacing(18)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        form.setLabelAlignment(Qt.AlignLeft)

        self.cb_fast = QCheckBox()
        self.cb_fast.setChecked(True)
        self.cb_fast.toggled.connect(self._on_fast_toggled)

        # Spinboxes with hard minimum sizes (theme.make_spinbox): Qt can't squash
        # them to a point — digits and arrows stay visible.
        self.sp_workers = theme.make_spinbox()
        self.sp_workers.setRange(1, 8)
        self.sp_workers.setValue(1)
        self.sp_workers.valueChanged.connect(self._refresh_warning)

        self.sp_connections = theme.make_spinbox()
        self.sp_connections.setRange(1, 16)
        self.sp_connections.setValue(4)   # see tg-downloader-tuning: account caps ~5 MB/s
        self.sp_connections.valueChanged.connect(self._refresh_warning)

        # Explicit wrapping QLabels instead of QFormLayout auto-labels — long user
        # wording isn't clipped by the window edge (anti-clipping).
        self.lbl_workers = QLabel()
        self.lbl_workers.setWordWrap(True)
        self.lbl_conns = QLabel()
        self.lbl_conns.setWordWrap(True)

        perf_lay.addWidget(self.cb_fast)
        form.addRow(self.lbl_workers, self.sp_workers)
        form.addRow(self.lbl_conns, self.sp_connections)
        perf_lay.addLayout(form)

        # Interactive total-DC-load banner.
        self.warning = theme.WarningBanner()
        perf_lay.addWidget(self.warning)
        self.hint_threads = theme.hint_label("")
        perf_lay.addWidget(self.hint_threads)
        root.addWidget(self.perf_box)

        root.addStretch(1)

        # ── action row (pinned: lives in `outer`, below the scroll area) ──
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACING)
        actions.setContentsMargins(theme.MARGIN, theme.SPACING, theme.MARGIN, theme.MARGIN)
        self.back_btn = QPushButton()
        self.back_btn.clicked.connect(self.back.emit)
        self.upload_btn = QPushButton()
        self.upload_btn.clicked.connect(self.request_upload.emit)

        self.spinner = theme.Spinner()
        self.status = QLabel("")
        self.status.setObjectName("statusInfo")
        self.status.setWordWrap(True)

        self.run_btn = QPushButton()
        self.run_btn.setProperty("primary", True)
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self._on_run)

        actions.addWidget(self.back_btn)
        actions.addWidget(self.upload_btn)
        actions.addStretch(1)
        actions.addWidget(self.spinner)
        actions.addWidget(self.status)
        actions.addWidget(self.run_btn)
        outer.addLayout(actions)

        self.retranslate()

    def retranslate(self) -> None:
        self.title.setText(t("opt_title"))
        self.cfg_box.setTitle(t("sec_config"))
        self.cb_download_all.setText(t("cb_download_all"))
        self.cb_download_all.setToolTip(t("tip_download_all"))
        self.hint_download_all.setText(t("hint_download_all"))
        self.proc_box.setTitle(t("sec_processing"))
        self.cb_keep_raw_video.setText(t("cb_keep_raw_video"))
        self.cb_keep_raw_video.setToolTip(t("tip_keep_raw_video"))
        self.cb_dedup.setText(t("cb_dedup"))
        self.cb_dedup.setToolTip(t("tip_dedup"))
        self.cb_quality.setText(t("cb_quality"))
        self.cb_quality.setToolTip(t("tip_quality"))
        self.hint_processing.setText(t("hint_processing"))
        self.links_box.setTitle(t("sec_links"))
        self.cb_download_links.setText(t("cb_download_links"))
        self.cb_download_links.setToolTip(t("tip_download_links"))
        self.lbl_link_format.setText(t("lbl_link_format"))
        self.cmb_link_format.setToolTip(t("tip_link_format"))
        self.hint_links.setText(t("hint_links"))
        self.perf_box.setTitle(t("sec_threads"))
        self.cb_fast.setText(t("cb_fast"))
        self.cb_fast.setToolTip(t("tip_fast"))
        self.sp_workers.setToolTip(t("tip_workers"))
        self.sp_connections.setToolTip(t("tip_connections"))
        self.lbl_workers.setText(t("lbl_workers"))
        self.lbl_conns.setText(t("lbl_conns"))
        self.hint_threads.setText(t("hint_threads"))
        self.back_btn.setText(t("nav_back"))
        self.upload_btn.setText(t("btn_upload_panel"))
        self.run_btn.setText(t("btn_run_export"))
        self._refresh_warning()

    # ── logic lock: "Download everything" overrides manual filter toggles ──
    def _on_download_all_toggled(self, on: bool) -> None:
        """The global collect mode manages dedup/quality itself (via review and
        force_include), so enabling "Download everything" forces both ON (ticked)
        and locks them; disabling the mode returns access."""
        for cb in (self.cb_dedup, self.cb_quality):
            if on:
                cb.setChecked(True)
            cb.setEnabled(not on)

    def _on_links_toggled(self, on: bool) -> None:
        """Format picker is meaningful only when link/comic download is enabled."""
        self.cmb_link_format.setEnabled(on)

    # ── interactive warning ────────────────────────────────────────────────────
    def _on_fast_toggled(self, on: bool) -> None:
        self.sp_connections.setEnabled(on)
        self._refresh_warning()

    def _refresh_warning(self) -> None:
        workers = self.sp_workers.value()
        per_file = self.sp_connections.value() if self.cb_fast.isChecked() else 1
        total = workers * per_file
        msg = t("warn_conn", total=total, workers=workers, per=per_file)
        if total > self.CONN_WARN_THRESHOLD:
            self.warning.set_message(msg + t("warn_conn_high"), warn=True)
        else:
            self.warning.set_message(msg + t("warn_conn_ok"), warn=False)

    # ── collect Options ─────────────────────────────────────────────────────────
    def build_options(self) -> "em.Options":
        # Affirmative checkboxes: value taken directly (ticked = enabled). Exception —
        # "keep raw video": ticked means DISABLE faststart.
        return em.Options(
            workers=self.sp_workers.value(),
            use_dedup=self.cb_dedup.isChecked(),
            use_fast=self.cb_fast.isChecked(),
            connections=self.sp_connections.value(),
            use_quality=self.cb_quality.isChecked(),
            faststart=not self.cb_keep_raw_video.isChecked(),
            download_links=self.cb_download_links.isChecked(),
            link_format=self.cmb_link_format.currentData() or "cbz",
        )

    def _on_run(self) -> None:
        opts = self.build_options()
        want_review = self.cb_download_all.isChecked()
        # Spinner + button lock turn on here, released by busy_changed(False).
        self.spinner.start()
        self.run_btn.setEnabled(False)
        self.status.setText(t("status_scanning") if want_review else t("status_starting"))
        self.request_export.emit(opts, want_review)

    def _on_busy(self, busy: bool) -> None:
        self.run_btn.setEnabled(not busy)
        self.upload_btn.setEnabled(not busy)
        self.back_btn.setEnabled(not busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
            self.status.setText("")
