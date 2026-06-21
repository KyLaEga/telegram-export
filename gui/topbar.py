"""Application header — Theme + Language switchers (TensorMedia-style top bar).

A slim bar pinned above the screen stack. The combos drive the two global
runtime settings:
  • Theme   → ``theme.set_theme`` + ``theme.refresh_custom`` (persisted in QSettings);
  • Language → ``i18n.translator.set_language`` (persisted by the engine).

The bar emits no business logic itself — ``MainWindow`` connects ``theme_changed``
and listens to ``translator.language_changed`` to retranslate every screen.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

from . import theme
from .i18n import t, translator


class TopBar(QWidget):
    theme_changed = Signal(bool)   # True → dark

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("topBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.MARGIN, 8, theme.MARGIN, 8)
        lay.setSpacing(8)

        self.brand = QLabel("✈  Telegram Export")
        self.brand.setStyleSheet("font-weight:700;font-size:14px;background:transparent;")
        lay.addWidget(self.brand)
        lay.addStretch(1)

        self.theme_lbl = QLabel()
        self.theme_lbl.setStyleSheet("background:transparent;")
        self.theme_cb = QComboBox()
        self.theme_cb.addItem("", True)    # dark
        self.theme_cb.addItem("", False)   # light
        self.theme_cb.setCurrentIndex(0 if theme.is_dark() else 1)
        self.theme_cb.activated.connect(self._on_theme)

        self.lang_lbl = QLabel()
        self.lang_lbl.setStyleSheet("background:transparent;")
        self.lang_cb = QComboBox()
        self.lang_cb.addItem("", "en")
        self.lang_cb.addItem("", "ru")
        self.lang_cb.setCurrentIndex(0 if translator.current_lang == "en" else 1)
        self.lang_cb.activated.connect(self._on_lang)

        lay.addWidget(self.theme_lbl)
        lay.addWidget(self.theme_cb)
        lay.addSpacing(6)
        lay.addWidget(self.lang_lbl)
        lay.addWidget(self.lang_cb)

        self.retranslate()

    def _on_theme(self, index: int) -> None:
        self.theme_changed.emit(bool(self.theme_cb.itemData(index)))

    def _on_lang(self, index: int) -> None:
        translator.set_language(self.lang_cb.itemData(index))

    def retranslate(self) -> None:
        self.theme_lbl.setText(t("topbar_theme"))
        self.lang_lbl.setText(t("topbar_lang"))
        self.theme_cb.setItemText(0, t("theme_dark"))
        self.theme_cb.setItemText(1, t("theme_light"))
        self.lang_cb.setItemText(0, t("lang_en"))
        self.lang_cb.setItemText(1, t("lang_ru"))
