"""Screen 1 — Login / configuration.

The api_id / api_hash / phone / channel / dest fields are read from `em.load_config()`
and saved by `em.save_config()` (the same config.json the CLI uses). "Sign in" starts
an interactive login via EngineController: a code field (with "Resend") is shown when
needed and, for 2FA, a cloud-password field.

The `done(cfg)` signal tells the app login succeeded and the wizard can advance.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

import export_media as em
from .. import theme
from ..i18n import t
from ..controller import EngineController


class LoginScreen(QWidget):
    done = Signal(dict)  # login complete; pass the current cfg onward

    def __init__(self, controller: EngineController, parent=None) -> None:
        super().__init__(parent)
        self._ctl = controller
        self._build()
        self._load_existing()
        self._wire_controller()

    # ── layout ──────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = theme.page_layout(self)
        self.title = theme.title_label(t("login_title"))
        root.addWidget(self.title)

        self.cfg_box, cfg_lay = theme.section(t("sec_config"))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(theme.SPACING)
        form.setVerticalSpacing(theme.SPACING)
        # Monolithic layout: value fields stretch to the right edge
        # (AllNonFixedFieldsGrow), and label+field NEVER wrap to separate rows
        # (DontWrapRows) — this prevents overlap when the window shrinks.
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)

        def _grow(edit: QLineEdit) -> QLineEdit:
            edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            return edit

        self.api_id = _grow(QLineEdit())
        self.api_id.setValidator(QIntValidator(0, 2_147_483_647, self))
        self.api_hash = _grow(QLineEdit())
        self.phone = _grow(QLineEdit())
        self.channel = _grow(QLineEdit())

        # ── destination folder: path (grows) + compact volume combo + button ──
        self.dest = _grow(QLineEdit())
        self.dest.setMinimumWidth(280)
        self.volumes = QComboBox()
        self.volumes.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.volumes.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._reload_volumes()
        self.volumes.activated.connect(self._pick_volume)
        self.browse_btn = QPushButton()
        self.browse_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.browse_btn.clicked.connect(self._browse_dest)
        dest_row = QHBoxLayout()
        dest_row.setContentsMargins(0, 0, 0, 0)
        dest_row.setSpacing(8)
        dest_row.addWidget(self.dest, 1)
        dest_row.addWidget(self.volumes, 0)
        dest_row.addWidget(self.browse_btn, 0)
        dest_box = QWidget()
        dest_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        dest_box.setLayout(dest_row)

        # Explicit labels (stored) so retranslate() can update them.
        self.lbl_api_id = QLabel()
        self.lbl_api_hash = QLabel()
        self.lbl_phone = QLabel()
        self.lbl_channel = QLabel()
        self.lbl_dest = QLabel()
        self.lbl_dest.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        form.addRow(self.lbl_api_id, self.api_id)
        form.addRow(self.lbl_api_hash, self.api_hash)
        form.addRow(self.lbl_phone, self.phone)
        form.addRow(self.lbl_channel, self.channel)
        form.addRow(self.lbl_dest, dest_box)
        cfg_lay.addLayout(form)
        root.addWidget(self.cfg_box)

        # ── action row (save / sign in) ──
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACING)
        self.save_btn = QPushButton()
        self.save_btn.clicked.connect(self._on_save)
        self.login_btn = QPushButton()
        self.login_btn.setProperty("primary", True)
        self.login_btn.setDefault(True)
        self.login_btn.clicked.connect(self._on_login)
        actions.addWidget(self.save_btn)
        actions.addStretch(1)
        actions.addWidget(self.login_btn)
        root.addLayout(actions)

        # ── code / 2FA confirmation block (hidden until a code is sent) ──
        self.auth_box = QFrame()
        self.auth_box.setFrameShape(QFrame.StyledPanel)
        ab = QVBoxLayout(self.auth_box)
        self.code = QLineEdit()
        self.code.returnPressed.connect(self._on_confirm_code)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.returnPressed.connect(self._on_confirm_password)
        self.password.hide()
        code_row = QHBoxLayout()
        self.confirm_btn = QPushButton()
        self.confirm_btn.clicked.connect(self._on_confirm_code)
        self.resend_btn = QPushButton()
        self.resend_btn.clicked.connect(self._ctl.resend_code)
        self.cancel_btn = QPushButton()
        self.cancel_btn.clicked.connect(self._ctl.cancel_login)
        code_row.addWidget(self.code, 1)
        code_row.addWidget(self.confirm_btn)
        code_row.addWidget(self.resend_btn)
        code_row.addWidget(self.cancel_btn)
        ab.addLayout(code_row)
        ab.addWidget(self.password)
        self.auth_box.hide()
        root.addWidget(self.auth_box)

        root.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("statusInfo")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.retranslate()

    def retranslate(self) -> None:
        self.title.setText(t("login_title"))
        self.cfg_box.setTitle(t("sec_config"))
        self.api_id.setPlaceholderText(t("ph_api_id"))
        self.api_hash.setPlaceholderText(t("ph_api_hash"))
        self.phone.setPlaceholderText(t("ph_phone"))
        self.channel.setPlaceholderText(t("ph_channel"))
        self.dest.setPlaceholderText(t("ph_dest"))
        self.browse_btn.setText(t("btn_browse"))
        self.lbl_api_id.setText(t("lbl_api_id"))
        self.lbl_api_hash.setText(t("lbl_api_hash"))
        self.lbl_phone.setText(t("lbl_phone"))
        self.lbl_channel.setText(t("lbl_channel"))
        self.lbl_dest.setText(t("lbl_dest"))
        self.save_btn.setText(t("btn_save"))
        self.login_btn.setText(t("btn_login"))
        self.code.setPlaceholderText(t("ph_code"))
        self.password.setPlaceholderText(t("ph_password"))
        self.confirm_btn.setText(t("btn_confirm"))
        self.resend_btn.setText(t("btn_resend"))
        self.cancel_btn.setText(t("btn_cancel"))
        if self.volumes.count():
            self.volumes.setItemText(0, t("volumes_placeholder"))

    # ── load/save configuration ─────────────────────────────────────────────
    def _load_existing(self) -> None:
        cfg = em.load_config()
        if not cfg:
            return
        self.api_id.setText(str(cfg.get("api_id", "")))
        self.api_hash.setText(cfg.get("api_hash", ""))
        self.phone.setText(cfg.get("phone", ""))
        self.channel.setText(cfg.get("channel", ""))
        self.dest.setText(cfg.get("dest", ""))

    def _collect(self) -> "dict | None":
        api_id = self.api_id.text().strip()
        cfg = {
            "api_id": api_id,
            "api_hash": self.api_hash.text().strip(),
            "phone": self.phone.text().strip(),
            "channel": self.channel.text().strip(),
            "dest": self.dest.text().strip(),
        }
        missing = [k for k, v in cfg.items() if not v]
        if missing:
            self._warn(t("msg_fill_fields", fields=", ".join(missing)))
            return None
        if not api_id.isdigit():
            self._warn(t("msg_api_id_numeric"))
            return None
        cfg["api_id"] = int(api_id)
        return cfg

    def _on_save(self) -> "dict | None":
        cfg = self._collect()
        if cfg is None:
            return None
        em.save_config(cfg)
        self._info(t("msg_config_saved", path=em.CONFIG_PATH))
        return cfg

    # ── destination folder ──
    def _reload_volumes(self) -> None:
        self.volumes.clear()
        self.volumes.addItem(t("volumes_placeholder"))
        for v in em.list_volumes():
            self.volumes.addItem(os.path.basename(v), v)

    def _pick_volume(self, index: int) -> None:
        vol = self.volumes.itemData(index)
        if vol:
            self.dest.setText(os.path.join(vol, "telegram_export"))

    def _browse_dest(self) -> None:
        start = self.dest.text().strip() or "/Volumes"
        chosen = QFileDialog.getExistingDirectory(self, t("dlg_dest_title"), start)
        if chosen:
            self.dest.setText(chosen)

    # ── login ────────────────────────────────────────────────────────────────
    def _on_login(self) -> None:
        cfg = self._on_save()  # login requires a valid saved configuration
        if cfg is None:
            return
        self._cfg = cfg
        self._set_login_busy(True)
        self._info(t("msg_connecting"))
        self._ctl.login(cfg)

    def _on_confirm_code(self) -> None:
        code = self.code.text().strip()
        if code:
            self._ctl.submit_code(code)

    def _on_confirm_password(self) -> None:
        pwd = self.password.text()
        if pwd:
            self._ctl.submit_password(pwd)

    def _wire_controller(self) -> None:
        self._ctl.login_code_sent.connect(self._on_code_sent)
        self._ctl.login_need_password.connect(self._on_need_password)
        self._ctl.login_ok.connect(self._on_login_ok)
        self._ctl.login_failed.connect(self._on_login_failed)
        self._ctl.login_cancelled.connect(self._on_login_cancelled)

    def _on_code_sent(self, descr: str) -> None:
        self.auth_box.show()
        self.password.hide()
        self.code.setFocus()
        self._info(t("msg_code_sent", descr=descr))

    def _on_need_password(self) -> None:
        self.password.show()
        self.password.setFocus()
        self._info(t("msg_need_password"))

    def _on_login_ok(self, greet: str) -> None:
        self._set_login_busy(False)
        self.auth_box.hide()
        self._info(t("msg_login_ok", greet=greet))
        self.done.emit(self._cfg)

    def _on_login_failed(self, message: str) -> None:
        # A wrong code/password is not necessarily fatal — keep the code form visible
        # and show the text non-modally (a modal on every wrong code would annoy).
        if not self.auth_box.isVisible():
            self._set_login_busy(False)
        self._error(message)

    def _on_login_cancelled(self) -> None:
        self._set_login_busy(False)
        self.auth_box.hide()
        self._info(t("msg_login_cancelled"))

    def _set_login_busy(self, busy: bool) -> None:
        self.login_btn.setEnabled(not busy)
        for w in (self.api_id, self.api_hash, self.phone, self.channel, self.dest):
            w.setReadOnly(busy)

    # ── status ──
    def _info(self, text: str) -> None:
        self.status.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        self.status.setText(text)

    def _error(self, text: str) -> None:
        self.status.setStyleSheet(f"color:{theme.DANGER};")
        self.status.setText(text)

    def _warn(self, text: str) -> None:
        self._error(text)
        QMessageBox.warning(self, t("warn_title"), text)
