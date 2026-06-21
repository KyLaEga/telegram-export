"""Screen 5 — Upload Pipeline panel (send a folder to a channel).

Fields: source folder (grows) + "Browse…" (Fixed), target channel (`--target`, grows).
4 checkboxes per uploader.UploadOptions:
  • skip already-uploaded (skip_uploaded, upload_state.db);
  • recurse into subfolders (recursive);
  • send as native media with preview (native_media → send_video/photo/…);
  • alphabetical order + file name in caption (caption_filename).

Launch goes through `EngineController.start_upload`; progress is taken from the single
`event(name, args)` stream (upload_*). The GUI thread never blocks.

UI: a dynamic pipeline status above the progress bar, a dark monospace "operations
log", verbose tooltips on the checkboxes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QWidget,
)

from .. import theme
from ..i18n import t
from ._fmt import fmt_speed, human_size


class UploadScreen(QWidget):
    back = Signal()

    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        self._ctl = controller
        self._cfg: dict = {}
        # Screen safety floor: props up minimumSizeHint of the QStackedWidget so the
        # operations log and button row don't drift past the window's bottom edge.
        self.setMinimumSize(760, 480)
        self._build()
        self._ctl.event.connect(self._on_event)
        self._ctl.run_done.connect(self._on_run_done)
        self._ctl.failed.connect(self._on_failed)
        self._ctl.busy_changed.connect(self._on_busy)

    def set_cfg(self, cfg: dict) -> None:
        """MainWindow passes cfg (api_id/hash/phone/channel) after login."""
        self._cfg = dict(cfg or {})
        if not self.target.text().strip():
            self.target.setText(str(self._cfg.get("channel", "")))
        if not self.folder.text().strip():
            self.folder.setText(str(self._cfg.get("dest", "")))

    # ── layout ──────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = theme.page_layout(self)
        self.title = theme.title_label(t("up_title"))
        root.addWidget(self.title)

        # ── Configuration: paths ──
        self.cfg_box, cfg_lay = theme.section(t("sec_config"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(theme.SPACING)
        form.setVerticalSpacing(theme.SPACING)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.folder = QLineEdit()
        self.folder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.browse_btn = QPushButton()
        self.browse_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.browse_btn.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.setSpacing(theme.SPACING)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(self.browse_btn, 0)
        folder_box = QWidget()
        folder_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        folder_box.setLayout(folder_row)

        self.target = QLineEdit()
        self.target.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.lbl_folder = QLabel()
        self.lbl_target = QLabel()
        form.addRow(self.lbl_folder, folder_box)
        form.addRow(self.lbl_target, self.target)
        cfg_lay.addLayout(form)
        root.addWidget(self.cfg_box)

        # ── Processing options (4 checkboxes with tooltip explanations) ──
        self.opt_box, opt_lay = theme.section(t("sec_processing"))
        self.cb_skip_uploaded = QCheckBox()
        self.cb_recursive = QCheckBox()
        self.cb_native = QCheckBox()
        self.cb_caption = QCheckBox()
        for cb in (self.cb_skip_uploaded, self.cb_recursive,
                   self.cb_native, self.cb_caption):
            cb.setChecked(True)   # UploadOptions defaults — all True
            opt_lay.addWidget(cb)
        root.addWidget(self.opt_box)

        # ── Current upload: dynamic status + progress ──
        self.prog_box, prog_lay = theme.section(t("sec_current_upload"))
        self.status_lbl = QLabel(t("up_idle"))
        self.status_lbl.setStyleSheet("font-weight: 600;")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        prog_lay.addWidget(self.status_lbl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        prog_lay.addWidget(self.bar)
        meta = QHBoxLayout()
        self.counts = QLabel()
        self.counts.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.cur_speed = QLabel("0 " + t("unit_speed"))
        self.cur_speed.setStyleSheet(f"font-weight: 600; color: {theme.SUCCESS};")
        meta.addWidget(self.counts)
        meta.addStretch(1)
        meta.addWidget(self.cur_speed)
        prog_lay.addLayout(meta)
        root.addWidget(self.prog_box)

        # ── Operations log (dark monospace console) ──
        self.log_header = QLabel(t("log_header_up"))
        self.log_header.setStyleSheet("font-weight: 700;")
        root.addWidget(self.log_header)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setStyleSheet(theme.CONSOLE_QSS)
        root.addWidget(self.log, 1)

        # ── actions ──
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACING)
        self.back_btn = QPushButton()
        self.back_btn.clicked.connect(self.back.emit)
        self.stop_btn = QPushButton()
        self.stop_btn.setProperty("danger", True)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        self.start_btn = QPushButton()
        self.start_btn.setProperty("primary", True)
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        actions.addWidget(self.back_btn)
        actions.addWidget(self.stop_btn)
        actions.addStretch(1)
        actions.addWidget(self.start_btn)
        root.addLayout(actions)

        self._sent = self._skipped = self._failed = 0
        self.retranslate()

    def retranslate(self) -> None:
        self.title.setText(t("up_title"))
        self.cfg_box.setTitle(t("sec_config"))
        self.folder.setPlaceholderText(t("ph_dest"))
        self.target.setPlaceholderText(t("ph_target"))
        self.browse_btn.setText(t("btn_browse"))
        self.lbl_folder.setText(t("lbl_src_folder"))
        self.lbl_target.setText(t("lbl_target"))
        self.opt_box.setTitle(t("sec_processing"))
        self.cb_skip_uploaded.setText(t("cb_skip_uploaded"))
        self.cb_skip_uploaded.setToolTip(t("tip_skip_uploaded"))
        self.cb_recursive.setText(t("cb_recursive"))
        self.cb_recursive.setToolTip(t("tip_recursive"))
        self.cb_native.setText(t("cb_native"))
        self.cb_native.setToolTip(t("tip_native"))
        self.cb_caption.setText(t("cb_caption"))
        self.cb_caption.setToolTip(t("tip_caption"))
        self.prog_box.setTitle(t("sec_current_upload"))
        self.log_header.setText(t("log_header_up"))
        self.back_btn.setText(t("nav_back"))
        self.stop_btn.setText(t("btn_stop_upload"))
        self.start_btn.setText(t("btn_start_upload"))
        self._refresh_counts()
        # Refresh the idle status text only when nothing is running.
        if not self.stop_btn.isEnabled():
            self.status_lbl.setText(t("up_idle"))

    # ── actions ───────────────────────────────────────────────────────────────
    def _browse(self) -> None:
        start = self.folder.text().strip() or "/Volumes"
        chosen = QFileDialog.getExistingDirectory(self, t("dlg_upload_folder"), start)
        if chosen:
            self.folder.setText(chosen)

    def _on_start(self) -> None:
        import uploader
        folder = self.folder.text().strip()
        target = self.target.text().strip()
        if not folder:
            self._append(t("up_need_folder"))
            return
        options = uploader.UploadOptions(
            recursive=self.cb_recursive.isChecked(),
            native_media=self.cb_native.isChecked(),
            skip_uploaded=self.cb_skip_uploaded.isChecked(),
            caption_filename=self.cb_caption.isChecked(),
        )
        self._reset_counts()
        if not self._ctl.start_upload(self._cfg, folder, target or None, options):
            self._append(t("up_busy"))
            return
        self._set_status(t("up_start_status"))
        self._append(t("up_start_log", folder=folder,
                       target=target or self._cfg.get("channel", "")))

    def _on_stop(self) -> None:
        self.stop_btn.setEnabled(False)
        self._set_status(t("up_stopping"))
        self._append(t("up_stop_requested"))
        self._ctl.stop()

    def _on_busy(self, busy: bool) -> None:
        self.start_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.back_btn.setEnabled(not busy)

    # ── engine events (upload_* only) ──────────────────────────────────────────
    def _on_event(self, name: str, args: tuple) -> None:
        # Shared event channel — handle only when the upload screen is active.
        if not self.isVisible():
            return
        if name == "upload_started":
            fname, size = args
            self._set_status(t("up_sending", fname=fname))
            self.bar.setValue(0)
        elif name == "upload_progress":
            _name, current, total, speed = args
            self.bar.setValue(int(1000 * current / total) if total else 0)
            self.cur_speed.setText(fmt_speed(speed))
        elif name == "upload_done":
            fname, size, speed, message_id = args
            self.bar.setValue(1000)
            self._sent += 1
            self._refresh_counts()
            self._append(t("up_done_line", fname=fname, size=human_size(size),
                           speed=fmt_speed(speed), msg=message_id))
        elif name == "upload_skipped":
            (fname,) = args
            self._skipped += 1
            self._refresh_counts()
            self._append(t("up_skipped_line", fname=fname))
        elif name == "upload_failed":
            fname, err = args
            self._failed += 1
            self._refresh_counts()
            self._append(t("up_failed_line", fname=fname, err=err))
        elif name == "upload_preview":
            summary = args[0]
            self._append(t("up_preview", n=summary.get("to_send", "?"),
                           chat=summary.get("chat_title", "")))
        elif name == "status" and args and args[0].strip():
            self._append(args[0].strip())

    def _set_status(self, text: str) -> None:
        self.status_lbl.setText(text)

    def _reset_counts(self) -> None:
        self._sent = self._skipped = self._failed = 0
        self.bar.setValue(0)
        self.cur_speed.setText("0 " + t("unit_speed"))
        self._set_status(t("up_start_status"))
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        self.counts.setText(t("up_counts", sent=self._sent,
                              skipped=self._skipped, failed=self._failed))

    def _on_run_done(self, _result: dict) -> None:
        if not self.isVisible():
            return
        self.stop_btn.setEnabled(False)
        self.bar.setValue(1000)
        self._set_status(t("up_finished", sent=self._sent,
                           skipped=self._skipped, failed=self._failed))
        self._append(t("up_finished_log"))

    def _on_failed(self, message: str) -> None:
        if not self.isVisible():
            return
        self.stop_btn.setEnabled(False)
        self._set_status(t("up_failed_status"))
        self._append(t("err_prefix", message=message))

    def _append(self, line: str) -> None:
        self.log.appendPlainText(line)
        self.log.moveCursor(QTextCursor.MoveOperation.End)
