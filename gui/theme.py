"""Unified visual layer (Enterprise UI) — TensorMedia design system.

Ports the Discord-style palette from the sibling TensorMedia app: a dark default
plus a light variant, switchable at runtime. The dominant accent is the TensorMedia
"blurple" (#5865F2) for focus / progress / selection, with green (#23A559) as the
primary call-to-action.

Here lives:
  • two semantic palettes (DARK / LIGHT) and the global ``QStyleSheet``;
  • the hard layout constants — ``MARGIN = 20``, ``SPACING = 12`` (used by ALL screens
    so margins and gaps are identical);
  • reusable widgets — ``Spinner`` (busy indicator), ``StatCard`` (status counter),
    ``WarningBanner`` (⚠️ banner with dynamic colour), ``section()`` (a ready QGroupBox),
    ``CONSOLE_QSS`` (dark monospace log style);
  • ``apply_theme(app, dark)`` / ``set_theme`` / ``refresh_custom`` — the runtime switch.

Screens MUST NOT hard-code theme-dependent colours — they read them here. Accent
colours (PRIMARY/SUCCESS/…) are theme-independent and safe to inline.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPalette
from PySide6.QtWidgets import (
    QApplication, QFrame, QGroupBox, QHBoxLayout, QLabel, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget,
)

# ── hard layout rules (mandatory for every form) ────────────────────────────────
MARGIN = 20      # inner container padding
SPACING = 12     # gap between elements

# ── accent colours — THEME-INDEPENDENT (identical in dark & light) ──────────────
PRIMARY = "#5865F2"          # blurple — focus, progress, selection, generic accent
PRIMARY_HOVER = "#4752C4"
PRIMARY_PRESSED = "#3c4499"
SUCCESS = "#23A559"          # green — primary call-to-action (Start/Sign-in/Apply)
SUCCESS_HOVER = "#1D8A4A"
INFO = "#3498db"
WARN = "#e0922f"
DANGER = "#ed4245"
PURPLE = "#9b6dd6"
TEAL = "#17a398"

# ── the two semantic palettes (TensorMedia values) ──────────────────────────────
DARK = {
    "bg": "#1E1F22",
    "surface": "#2B2D31",
    "border": "#4E5058",
    "text": "#DBDEE1",
    "muted": "#949BA4",
    "input_bg": "#1E1F22",
    "btn_bg": "#404249",
    "btn_hover": "#4E5058",
    "btn_pressed": "#313338",
    "btn_disabled_bg": "#313338",
    "btn_disabled_text": "#5C5E66",
    "table_alt": "#313338",
    "table_sel": "#404478",
    "table_sel_text": "#FFFFFF",
    "header_bg": "#1E1F22",
    "gridline": "#232428",
    "scroll_handle": "#404249",
    "scroll_handle_hover": "#4E5058",
    "warn_bg": "#3a2f1e",
    "warn_border": "#6e5a32",
    "warn_text": "#e0a94a",
    "info_bg": "#1e2c3a",
    "info_border": "#2f4a66",
    "info_text": "#5dade2",
}
LIGHT = {
    "bg": "#F2F3F5",
    "surface": "#FFFFFF",
    "border": "#E3E5E8",
    "text": "#313338",
    "muted": "#5C5E66",
    "input_bg": "#FFFFFF",
    "btn_bg": "#E3E5E8",
    "btn_hover": "#D4D7DC",
    "btn_pressed": "#B5BAC1",
    "btn_disabled_bg": "#EDEEF0",
    "btn_disabled_text": "#A0A4AB",
    "table_alt": "#F7F8FA",
    "table_sel": "#D9E8FF",
    "table_sel_text": "#313338",
    "header_bg": "#EEF0F3",
    "gridline": "#E7E9EE",
    "scroll_handle": "#C4C9D1",
    "scroll_handle_hover": "#ADB3BD",
    "warn_bg": "#fff4e2",
    "warn_border": "#f0c27a",
    "warn_text": "#b3700f",
    "info_bg": "#eef3fb",
    "info_border": "#cfe0f6",
    "info_text": "#2471a3",
}

# Dark "terminal" log — intentionally THEME-INDEPENDENT (always dark for the
# operations-console aesthetic), so the monospace log reads like a real terminal.
CONSOLE_QSS = (
    "QPlainTextEdit{background:#1A1B1E;color:#D6DAE0;border:1px solid #2c2f37;"
    "border-radius:6px;font-family:'SF Mono','Menlo','Courier New',monospace;"
    "font-size:11pt;padding:8px;selection-background-color:#5865F2;}"
)

# ── active state (mutated by _activate) ─────────────────────────────────────────
_IS_DARK = True
_P = DARK
# Theme-dependent module constants the screens read via ``theme.<NAME>``. Reassigned
# by _activate(); attribute access from screens always sees the current value.
BG = DARK["bg"]
SURFACE = DARK["surface"]
BORDER = DARK["border"]
TEXT = DARK["text"]
TEXT_MUTED = DARK["muted"]
SECONDARY_BG = DARK["btn_bg"]
SECONDARY_HOVER = DARK["btn_hover"]
WARN_BG = DARK["warn_bg"]
WARN_BORDER = DARK["warn_border"]
STYLESHEET = ""


def _build_qss(p: dict) -> str:
    return f"""
QWidget {{
    background: {p['bg']};
    color: {p['text']};
    font-family: -apple-system, 'SF Pro Text', 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QLabel {{ background: transparent; }}
QToolTip {{
    background: {p['surface']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: 6px; padding: 6px 8px;
}}

/* Semantic text labels (theme-driven so a theme switch recolours them live) */
QLabel#h1Title {{ font-size: 20px; font-weight: 700; color: {p['text']}; }}
QLabel#hint    {{ color: {p['muted']}; font-size: 11px; }}
QLabel#statusInfo {{ color: {p['muted']}; }}
QLabel#statusErr  {{ color: {DANGER}; }}

/* Container groups */
QGroupBox {{
    background: {p['surface']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    margin-top: 14px;
    padding: 14px {MARGIN}px {MARGIN}px {MARGIN}px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 6px;
    color: {p['text']};
    font-size: 13px;
}}

/* Buttons — secondary by default */
QPushButton {{
    background: {p['btn_bg']};
    border: 1px solid {p['border']};
    border-radius: 7px;
    padding: 8px 16px;
    color: {p['text']};
    font-weight: 500;
}}
QPushButton:hover {{ background: {p['btn_hover']}; }}
QPushButton:pressed {{ background: {p['btn_pressed']}; }}
QPushButton:disabled {{ color: {p['btn_disabled_text']}; background: {p['btn_disabled_bg']}; }}

/* Primary call-to-action (green) */
QPushButton[primary="true"], QPushButton:default {{
    background: {SUCCESS};
    border: 1px solid {SUCCESS};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[primary="true"]:hover, QPushButton:default:hover {{ background: {SUCCESS_HOVER}; border-color: {SUCCESS_HOVER}; }}
QPushButton[primary="true"]:pressed, QPushButton:default:pressed {{ background: #176b39; }}
QPushButton[primary="true"]:disabled {{ background: {p['btn_disabled_bg']}; border-color: {p['border']}; color: {p['btn_disabled_text']}; }}

/* Dangerous action (stop) */
QPushButton[danger="true"] {{
    background: transparent; border: 1px solid {DANGER}; color: {DANGER}; font-weight: 600;
}}
QPushButton[danger="true"]:hover {{ background: {DANGER}; color: #ffffff; }}
QPushButton[danger="true"]:disabled {{ background: {p['btn_disabled_bg']}; color: {p['btn_disabled_text']}; border-color: {p['border']}; }}

/* Inputs */
QLineEdit, QSpinBox, QComboBox {{
    background: {p['input_bg']};
    border: 1px solid {p['border']};
    border-radius: 6px;
    padding: 6px 8px;
    color: {p['text']};
    selection-background-color: {PRIMARY};
    selection-color: #ffffff;
}}
QSpinBox {{ min-height: 30px; padding: 4px 10px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 18px; border-left: 1px solid {p['border']}; background: {p['btn_bg']}; }}
QSpinBox::up-button {{ subcontrol-position: top right; margin: 1px 1px 0 0; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; margin: 0 1px 1px 0; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {PRIMARY}; }}
QLineEdit:read-only {{ background: {p['btn_disabled_bg']}; color: {p['muted']}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 20px; border: none; }}
QComboBox QAbstractItemView {{
    background: {p['surface']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: 6px; padding: 4px; outline: none;
    selection-background-color: {PRIMARY}; selection-color: #ffffff;
}}

/* Checkboxes */
QCheckBox {{ spacing: 8px; background: transparent; padding: 2px 0; }}
QCheckBox::indicator {{
    width: 17px; height: 17px; border-radius: 4px;
    border: 1px solid {p['border']}; background: {p['input_bg']};
}}
QCheckBox::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY}; }}
QCheckBox::indicator:hover {{ border-color: {PRIMARY}; }}
QCheckBox:disabled {{ color: {p['btn_disabled_text']}; }}

/* Progress bars */
QProgressBar {{
    background: {p['input_bg']}; border: none; border-radius: 6px; height: 12px;
    text-align: center; color: {p['text']}; font-size: 10px;
}}
QProgressBar::chunk {{ background: {PRIMARY}; border-radius: 6px; }}

/* Tables */
QTableView {{
    background: {p['surface']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    gridline-color: {p['gridline']};
    selection-background-color: {p['table_sel']};
    selection-color: {p['table_sel_text']};
    alternate-background-color: {p['table_alt']};
}}
QHeaderView::section {{
    background: {p['header_bg']};
    color: {p['muted']};
    border: none;
    border-right: 1px solid {p['border']};
    border-bottom: 1px solid {p['border']};
    padding: 7px 8px;
    font-weight: 600;
}}
QTableView QTableCornerButton::section {{ background: {p['header_bg']}; border: none; }}

/* Scrollbars — thin neutral */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p['scroll_handle']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p['scroll_handle_hover']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p['scroll_handle']}; border-radius: 5px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {p['scroll_handle_hover']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""


def _activate(dark: bool) -> None:
    """Recompute the active palette + module constants + global stylesheet."""
    global _IS_DARK, _P, STYLESHEET
    global BG, SURFACE, BORDER, TEXT, TEXT_MUTED, SECONDARY_BG, SECONDARY_HOVER
    global WARN_BG, WARN_BORDER
    _IS_DARK = dark
    _P = DARK if dark else LIGHT
    BG, SURFACE, BORDER = _P["bg"], _P["surface"], _P["border"]
    TEXT, TEXT_MUTED = _P["text"], _P["muted"]
    SECONDARY_BG, SECONDARY_HOVER = _P["btn_bg"], _P["btn_hover"]
    WARN_BG, WARN_BORDER = _P["warn_bg"], _P["warn_border"]
    STYLESHEET = _build_qss(_P)


_activate(True)  # default: dark


def is_dark() -> bool:
    return _IS_DARK


def set_theme(app: QApplication, dark: bool) -> None:
    """Apply the dark/light theme to the application (palette + global QSS)."""
    _activate(dark)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(_P["bg"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(_P["text"]))
    pal.setColor(QPalette.ColorRole.Base, QColor(_P["input_bg"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(_P["table_alt"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(_P["text"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(_P["btn_bg"]))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(_P["text"]))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(_P["surface"]))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(_P["text"]))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(PRIMARY))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)


def apply_theme(app: QApplication, dark: bool = True) -> None:
    """Attach the global style to QApplication (default: dark)."""
    set_theme(app, dark)


def refresh_custom(root: QWidget) -> None:
    """Re-style inline-styled custom widgets after a theme switch.

    Global-QSS-driven widgets recolour automatically when the stylesheet is
    re-applied; the few widgets that paint with theme-dependent inline styles
    (StatCard / WarningBanner / Spinner) expose ``_restyle()`` and are refreshed
    here by walking the widget tree.
    """
    for w in (root, *root.findChildren(QWidget)):
        restyle = getattr(w, "_restyle", None)
        if callable(restyle):
            restyle()


# ── reusable widgets ────────────────────────────────────────────────────────────
def page_layout(widget: QWidget) -> QVBoxLayout:
    """Root vertical layout with the mandatory MARGIN/SPACING."""
    lay = QVBoxLayout(widget)
    lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
    lay.setSpacing(SPACING)
    return lay


def section(title: str) -> "tuple[QGroupBox, QVBoxLayout]":
    """A ready QGroupBox container with a title and correct inner padding."""
    box = QGroupBox(title)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(MARGIN, SPACING, MARGIN, MARGIN)
    lay.setSpacing(SPACING)
    return box, lay


def make_spinbox() -> QSpinBox:
    """QSpinBox with guaranteed minimum size (anti-clipping)."""
    sp = QSpinBox()
    sp.setMinimumWidth(90)
    sp.setMinimumHeight(32)
    sp.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return sp


def title_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("h1Title")   # styled (and recoloured on theme switch) via QSS
    return lbl


def hint_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


class Spinner(QWidget):
    """Tidy busy indicator (12 fading rays, smooth rotation). Visible only while
    running — ``start()``/``stop()`` drive both animation and visibility."""

    def __init__(self, parent=None, diameter: int = 18, color: str = PRIMARY) -> None:
        super().__init__(parent)
        self._angle = 0
        self._color = QColor(color)
        self.setFixedSize(diameter, diameter)
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._rotate)
        self.hide()

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.show()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _rotate(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(self._angle)
        n = 12
        r_out = min(self.width(), self.height()) / 2 - 1
        r_in = r_out * 0.45
        pen = QPen()
        pen.setWidth(2)
        pen.setCapStyle(Qt.RoundCap)
        for i in range(n):
            c = QColor(self._color)
            c.setAlphaF((i + 1) / n)
            pen.setColor(c)
            p.setPen(pen)
            p.drawLine(0, int(r_in), 0, int(r_out))
            p.rotate(360 / n)


class StatCard(QFrame):
    """Large status counter: value + caption, in a card with an accent stripe."""

    def __init__(self, caption: str, color: str) -> None:
        super().__init__()
        self._color = color
        self.setObjectName("statCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        self._value = QLabel("0")
        self._cap = QLabel(caption)
        self._cap.setWordWrap(True)
        lay.addWidget(self._value)
        lay.addWidget(self._cap)
        self._restyle()

    def _restyle(self) -> None:
        self.setStyleSheet(
            f"#statCard{{background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:8px;border-left:4px solid {self._color};}}")
        self._value.setStyleSheet(
            f"font-size:24px;font-weight:700;color:{self._color};background:transparent;")
        self._cap.setStyleSheet(
            f"font-size:11px;color:{TEXT_MUTED};background:transparent;")

    def set_caption(self, caption: str) -> None:
        self._cap.setText(caption)

    def set_value(self, n: int) -> None:
        self._value.setText(str(n))


class WarningBanner(QFrame):
    """Info / warning frame with an icon and dynamic colour.

    ``set_message(text, warn=True)`` — orange ⚠️ mode; ``warn=False`` — neutral info
    mode. Colours come from the active palette, so a theme switch recolours it.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("banner")
        self._warn = False
        self._text_value = ""
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(10)
        self._icon = QLabel("ℹ️")
        self._icon.setStyleSheet("background:transparent;font-size:14px;")
        self._text = QLabel("")
        self._text.setWordWrap(True)
        lay.addWidget(self._icon, 0, Qt.AlignTop)
        lay.addWidget(self._text, 1)
        self._restyle()

    def set_message(self, text: str, warn: bool) -> None:
        self._text_value = text
        self._warn = warn
        self._restyle()

    def _restyle(self) -> None:
        self._text.setText(self._text_value)
        if self._warn:
            self._icon.setText("⚠️")
            self.setStyleSheet(
                f"#banner{{background:{_P['warn_bg']};border:1px solid {_P['warn_border']};"
                f"border-radius:7px;}}")
            self._text.setStyleSheet(
                f"background:transparent;color:{_P['warn_text']};font-weight:600;")
        else:
            self._icon.setText("ℹ️")
            self.setStyleSheet(
                f"#banner{{background:{_P['info_bg']};border:1px solid {_P['info_border']};"
                f"border-radius:7px;}}")
            self._text.setStyleSheet(
                f"background:transparent;color:{_P['info_text']};font-weight:500;")
