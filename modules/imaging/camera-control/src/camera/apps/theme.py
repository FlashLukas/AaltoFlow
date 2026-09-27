"""Theme -- dark/light palettes with a startup-selected active palette.

All GUI colours come from ONE module-level ``COLORS`` dict.  ``set_theme(name)``
swaps the active palette by mutating ``COLORS`` IN PLACE (clear + update), never
by rebinding it -- so any ``from .theme import COLORS`` (and the live lookups
below) keep seeing the active values.  Call ``set_theme(cfg.ui.theme)`` ONCE at
startup, before any widget is built.

This module's identity accent stays amber (matching the clMag-control family); the
LIGHT palette uses a slightly deeper amber so it stays legible on white.  The
neutral colours are shared verbatim across all the suite's modules so they match.

Legacy access: the painted widgets read colours by the old UPPERCASE names
(``theme.ACCENT``, ``theme.BG`` …).  A module ``__getattr__`` resolves those to
``COLORS[key]`` LIVE at each access, so once ``set_theme`` has run they paint in
the active palette automatically (no baked-in constants).
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from pathlib import Path

# --------------------------------------------------------------------------- #
# Palettes.  Neutrals are EXACT and shared across every module so they match;
# accent = this module's amber identity (LIGHT uses a deeper amber for contrast).
# --------------------------------------------------------------------------- #
DARK = {
    "bg": "#0e1013", "panel": "#171a1f", "panel_hi": "#1e222a", "border": "#2a2f37",
    "text": "#e8eaed", "muted": "#8b929c", "ok": "#3ddc84", "danger": "#ff5c5c",
    "grid": "#20242b", "pressed": "#402a12", "code_bg": "#0a0c0f",
    "accent": "#ff9e2c", "accent_hi": "#ffb454", "accent_dim": "#a8641a",
    "danger_hi": "#ff7a7a",
}
LIGHT = {
    "bg": "#f3f4f6", "panel": "#ffffff", "panel_hi": "#eceef1", "border": "#d3d7de",
    "text": "#1b1e24", "muted": "#6b7280", "ok": "#1a9e57", "danger": "#d13b3b",
    "grid": "#e3e6ea", "pressed": "#e6d7bd", "code_bg": "#f0f1f3",
    "accent": "#d9821a", "accent_hi": "#c26a12", "accent_dim": "#a05e12",
    "danger_hi": "#e05a5a",
}
THEMES = {"dark": DARK, "light": LIGHT}

# Active palette (mutated in place by set_theme -- never rebound).
COLORS = dict(DARK)


def set_theme(name: str) -> None:
    """Swap the active palette IN PLACE so shared references stay valid."""
    COLORS.clear()
    COLORS.update(THEMES.get((name or "dark").lower(), DARK))


# Old UPPERCASE colour names -> COLORS keys, resolved LIVE via module __getattr__.
_ALIASES = {
    "BG": "bg", "PANEL": "panel", "PANEL_HI": "panel_hi", "BORDER": "border",
    "TEXT": "text", "MUTED": "muted", "OK": "ok", "DANGER": "danger",
    "GRID": "grid", "PRESSED": "pressed", "CODE_BG": "code_bg",
    "ACCENT": "accent", "ACCENT_HI": "accent_hi", "ACCENT_DIM": "accent_dim",
    "DANGER_HI": "danger_hi",
}


def __getattr__(name: str):
    key = _ALIASES.get(name)
    if key is not None:
        return COLORS[key]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --------------------------------------------------------------------------- #
def apply_palette(app) -> None:
    """Set a matching Fusion QPalette from COLORS (so scroll areas + popups follow
    the theme).  Use with ``app.setStyle("Fusion")``."""
    c = COLORS
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(c["bg"]))
    pal.setColor(QPalette.WindowText, QColor(c["text"]))
    pal.setColor(QPalette.Base, QColor(c["panel_hi"]))
    pal.setColor(QPalette.AlternateBase, QColor(c["panel"]))
    pal.setColor(QPalette.Text, QColor(c["text"]))
    pal.setColor(QPalette.Button, QColor(c["panel_hi"]))
    pal.setColor(QPalette.ButtonText, QColor(c["text"]))
    pal.setColor(QPalette.ToolTipBase, QColor(c["panel"]))
    pal.setColor(QPalette.ToolTipText, QColor(c["text"]))
    pal.setColor(QPalette.Highlight, QColor(c["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor("#141414"))
    pal.setColor(QPalette.PlaceholderText, QColor(c["muted"]))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(c["muted"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(c["muted"]))
    app.setPalette(pal)


# Legacy alias (older call sites used apply_dark_palette).
apply_dark_palette = apply_palette


def build_stylesheet() -> str:
    """Return the Qt stylesheet as an f-string built from the ACTIVE COLORS."""
    c = COLORS
    return f"""
QWidget {{
    background: {c['bg']};
    color: {c['text']};
    font-size: 13px;
}}
QFrame#card {{
    background: {c['panel']};
    border: 1px solid {c['border']};
    border-radius: 10px;
}}
QLabel#cardTitle {{
    color: {c['muted']};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 2px;
}}
QLabel#bigValue {{
    font-size: 34px;
    font-weight: 700;
    color: {c['text']};
}}
QLabel#caption {{
    color: {c['accent_hi']};
    font-size: 11px;
}}
QLabel#muted {{ color: {c['muted']}; }}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background: {c['panel_hi']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 4px 6px;
    selection-background-color: {c['accent']};
    selection-color: #141414;
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {c['accent']};
}}
QPushButton {{
    background: {c['panel_hi']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 12px;
}}
QPushButton:hover {{ background: {c['border']}; }}
QPushButton:pressed {{ background: {c['pressed']}; }}
QPushButton:checked {{
    background: {c['accent']};
    color: #141414;
    border: 1px solid {c['accent']};
    font-weight: 700;
}}
QPushButton#primary {{
    background: {c['accent']};
    color: #141414;
    border: 1px solid {c['accent']};
    font-weight: 700;
}}
QPushButton#primary:hover {{ background: {c['accent_hi']}; }}
QPushButton#danger {{
    background: {c['danger']};
    color: #141414;
    border: 1px solid {c['danger']};
    font-weight: 700;
}}
QPushButton#danger:hover {{ background: {c['danger_hi']}; }}
/* A disabled button must LOOK disabled: the stage controls grey out while the
   stage service is not answering, and an amber "Find focus" that ignores
   clicks reads as a bug. Last, so it wins over #primary / #danger. */
QPushButton:disabled, QPushButton#primary:disabled, QPushButton#danger:disabled {{
    background: {c['panel']};
    color: {c['muted']};
    border: 1px dashed {c['border']};
    font-weight: 400;
}}
QDoubleSpinBox:disabled, QSpinBox:disabled {{ color: {c['muted']}; }}
QCheckBox {{
    color: {c['text']};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border: 2px solid {c['muted']};
    border-radius: 4px;
    background: {c['panel_hi']};
}}
QCheckBox::indicator:hover {{
    border: 2px solid {c['accent_hi']};
}}
QCheckBox::indicator:checked {{
    background: {c['accent']};
    border: 2px solid {c['accent']};
}}
QCheckBox::indicator:checked:hover {{
    background: {c['accent_hi']};
    border: 2px solid {c['accent_hi']};
}}
QCheckBox::indicator:disabled {{
    border: 2px solid {c['border']};
    background: {c['panel']};
}}
QTableWidget {{
    background: {c['panel']};
    gridline-color: {c['grid']};
    border: 1px solid {c['border']};
    border-radius: 8px;
}}
QHeaderView::section {{
    background: {c['panel_hi']};
    color: {c['muted']};
    border: none;
    padding: 4px;
}}
QPlainTextEdit#log {{
    background: {c['code_bg']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    font-family: Consolas, "DejaVu Sans Mono", monospace;
    font-size: 12px;
}}
QScrollArea {{ border: none; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 8px; }}
QTabBar::tab {{
    background: {c['panel']};
    padding: 6px 12px;
    border: 1px solid {c['border']};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{ background: {c['panel_hi']}; color: {c['accent_hi']}; }}
"""


def repolish(widget) -> None:
    """Re-apply the stylesheet after changing a widget's objectName at runtime."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# ─────────────────────────── taskbar / window icon ────────────────────────
#: This module's own icon.svg -- the very drawing mission-control puts on its
#: card, so the window, the launcher and the Start menu all agree.
ICON_FILE = Path(__file__).resolve().parents[3] / "icon.svg"


def apply_window_icon(app) -> None:
    """Give the application its module icon, in the title bar and the taskbar.

    Two steps, and on Windows BOTH are needed:
      * setWindowIcon() is what Qt draws in the title bar and in Alt-Tab.
      * an explicit AppUserModelID, or Windows groups every pythonw process
        under one generic "Python" taskbar button and shows Python's icon no
        matter what Qt was told.

    ORDER MATTERS, and not claiming the ID at all matters more. Windows caches
    the icon against the AppUserModelID, not against the process: a single run
    that claims an ID and then sets no icon leaves that ID showing the blank
    window icon FOREVER, and a later run that does have the icon does not undo
    it. Measured 2026-09-23 on the lab PC -- the measurement suite was stuck
    exactly that way, while the same code under an unused ID drew the icon at
    once. So: no icon file, no ID.

    Never fatal: a platform without that shell call simply keeps the default.
    """
    if not ICON_FILE.exists():
        return
    from PySide6 import QtGui
    app.setWindowIcon(QtGui.QIcon(str(ICON_FILE)))
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"Aalto.AaltoFlow.{ICON_FILE.parent.name}")
    except Exception:          # not Windows, or the call is unavailable
        pass
