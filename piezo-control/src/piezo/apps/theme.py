"""Shared theme (dark/amber + light) — one COLORS dict drives everything (§7).

This module follows the suite-wide theming pattern (as in ``clMag-control``):
every GUI colour is read from a single module-level ``COLORS`` dict.  Two
palettes are provided, :data:`DARK` and :data:`LIGHT`; :func:`set_theme` swaps
the active palette **by mutating COLORS in place** (never rebinding it), because
other modules do ``from .theme import COLORS`` and must keep seeing the live
values after a switch.

Theme is a STARTUP setting: :func:`set_theme` is called once, before any widget
is built (see ``gui.run_app``).  There is no live toggle.

Neutral colours are identical across all instrument modules so the windows
match; only the accent carries this module's identity (amber, like clMag).

Usage (from run_app):
    set_theme(cfg.ui.theme)          # BEFORE building widgets
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())
"""

from __future__ import annotations
from pathlib import Path

from PySide6.QtGui import QColor, QPalette

# --------------------------------------------------------------------------- #
# palettes  (neutrals shared across the suite; accent = this module's amber)
# --------------------------------------------------------------------------- #
DARK = {
    "bg": "#0e1013", "panel": "#171a1f", "panel_hi": "#1e222a", "border": "#2a2f37",
    "text": "#e8eaed", "muted": "#8b929c", "ok": "#3ddc84", "danger": "#ff5c5c",
    "grid": "#20242b", "pressed": "#402a12", "code_bg": "#0a0c0f",
    # accent = this module's dark accent (amber, matching clMag):
    "accent": "#ff9e2c", "accent_hi": "#ffb454", "accent_dim": "#a8641a",
}
LIGHT = {
    "bg": "#f3f4f6", "panel": "#ffffff", "panel_hi": "#eceef1", "border": "#d3d7de",
    "text": "#1b1e24", "muted": "#6b7280", "ok": "#1a9e57", "danger": "#d13b3b",
    "grid": "#e3e6ea", "pressed": "#e6d7bd", "code_bg": "#f0f1f3",
    # accent = a deeper amber so it stays legible on white:
    "accent": "#d9821a", "accent_hi": "#c26a12", "accent_dim": "#a05e12",
}
THEMES = {"dark": DARK, "light": LIGHT}

# The ACTIVE palette.  Mutated in place by set_theme() so `from .theme import
# COLORS` references stay valid after a switch.
COLORS = dict(DARK)


def set_theme(name: str) -> None:
    """Swap the active palette IN PLACE (dark by default).  Startup-only."""
    COLORS.clear()
    COLORS.update(THEMES.get((name or "dark").lower(), DARK))


# --------------------------------------------------------------------------- #
# Qt palette (so Fusion widgets, scroll areas and popups follow the theme)
# --------------------------------------------------------------------------- #
def apply_palette(app) -> None:
    """Set a QPalette matching the active COLORS.  Use with Fusion style."""
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
    pal.setColor(QPalette.HighlightedText, QColor("#101010"))
    pal.setColor(QPalette.PlaceholderText, QColor(c["muted"]))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(c["muted"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(c["muted"]))
    app.setPalette(pal)


# --------------------------------------------------------------------------- #
# stylesheet (built from the ACTIVE palette, so it reflects the chosen theme)
# --------------------------------------------------------------------------- #
def build_stylesheet() -> str:
    """Return the Qt stylesheet as an f-string built from the active COLORS."""
    c = COLORS
    return f"""
QWidget {{
    background: {c["bg"]};
    color: {c["text"]};
    font-size: 13px;
}}
QFrame#card {{
    background: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 10px;
}}
QLabel#cardTitle {{
    color: {c["muted"]};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 2px;
}}
QLabel#bigValue {{
    font-size: 34px;
    font-weight: 700;
    color: {c["text"]};
}}
QLabel#caption {{
    color: {c["accent_hi"]};
    font-size: 11px;
}}
QLabel#muted {{ color: {c["muted"]}; }}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background: {c["panel_hi"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    padding: 4px 6px;
    selection-background-color: {c["accent"]};
    selection-color: #101010;
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {c["accent"]};
}}
QPushButton {{
    background: {c["panel_hi"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
    padding: 6px 12px;
}}
QPushButton:hover {{ background: {c["border"]}; }}
QPushButton:pressed {{ background: {c["pressed"]}; }}
QPushButton:checked {{
    background: {c["accent"]};
    color: #141414;
    border: 1px solid {c["accent"]};
    font-weight: 700;
}}
QPushButton#primary {{
    background: {c["accent"]};
    color: #141414;
    border: 1px solid {c["accent"]};
    font-weight: 700;
}}
QPushButton#primary:hover {{ background: {c["accent_hi"]}; }}
QPushButton#danger {{
    background: {c["danger"]};
    color: #ffffff;
    border: 1px solid {c["danger"]};
    font-weight: 700;
}}
QPushButton#danger:hover {{ background: {c["danger"]}; }}
QTableWidget {{
    background: {c["panel"]};
    gridline-color: {c["grid"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}
QHeaderView::section {{
    background: {c["panel_hi"]};
    color: {c["muted"]};
    border: none;
    padding: 4px;
}}
QPlainTextEdit#log {{
    background: {c["code_bg"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    font-family: Consolas, "DejaVu Sans Mono", monospace;
    font-size: 12px;
}}
QTabWidget::pane {{ border: 1px solid {c["border"]}; border-radius: 8px; }}
QTabBar::tab {{
    background: {c["panel"]};
    padding: 6px 12px;
    border: 1px solid {c["border"]};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{ background: {c["panel_hi"]}; color: {c["accent_hi"]}; }}
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
