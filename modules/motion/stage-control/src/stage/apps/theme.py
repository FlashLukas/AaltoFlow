"""Theme: dark and light palettes with an amber accent (§7 of the guide).

Everything visual sources its colours from the module-level ``COLORS`` dict.
``set_theme("dark"|"light")`` swaps the palette IN PLACE (it mutates the same
dict object with ``clear()`` + ``update()``), so any module that did
``from .theme import COLORS`` -- or ``from . import theme`` and reads
``theme.COLORS[...]`` -- keeps seeing the *active* values.  Call ``set_theme``
once at startup, BEFORE building any widget.

``build_stylesheet()`` returns the Qt style sheet for the active palette (it is
an f-string over ``COLORS``), and ``apply_palette(app)`` sets a matching
``QPalette`` -- use it with the Fusion style so even scroll-area viewports and
popups follow the theme.

This module keeps the stage's own amber accent for identity; only the neutral
colours differ between the shared modules.  The light accent is a slightly
deeper amber so it stays legible on white.
"""

from __future__ import annotations
from pathlib import Path

# ---- the two palettes ----------------------------------------------------- #
# NOTE: the neutral keys here are identical across every module in the suite so
# the windows match; only accent/accent_hi/accent_dim carry this module's colour.

DARK = {
    "bg": "#0e1013", "panel": "#171a1f", "panel_hi": "#1e222a", "border": "#2a2f37",
    "text": "#e8eaed", "muted": "#8b929c", "ok": "#3ddc84", "danger": "#ff5c5c",
    "grid": "#20242b", "pressed": "#402a12", "code_bg": "#0a0c0f",
    # accent = this module's dark accent (amber, same as clMag):
    "accent": "#ff9e2c", "accent_hi": "#ffb454", "accent_dim": "#a8641a",
}

LIGHT = {
    "bg": "#f3f4f6", "panel": "#ffffff", "panel_hi": "#eceef1", "border": "#d3d7de",
    "text": "#1b1e24", "muted": "#6b7280", "ok": "#1a9e57", "danger": "#d13b3b",
    "grid": "#e3e6ea", "pressed": "#e6d7bd", "code_bg": "#f0f1f3",
    # accent = a deeper version of the dark accent, legible on white:
    "accent": "#d9821a", "accent_hi": "#c26a12", "accent_dim": "#a05e12",
}

THEMES = {"dark": DARK, "light": LIGHT}

# Active palette (starts dark).  Mutated IN PLACE by set_theme -- never rebound,
# so importers that captured this dict object keep pointing at the live values.
COLORS = dict(DARK)


def set_theme(name: str) -> None:
    """Swap the active palette in place (dark/light).  Unknown name -> dark."""
    COLORS.clear()
    COLORS.update(THEMES.get((name or "dark").lower(), DARK))


# ---- Qt palette ----------------------------------------------------------- #
def apply_palette(app) -> None:
    """Set a QPalette from the active COLORS so widgets the stylesheet doesn't
    reach (scroll-area viewports, popups) still follow the theme.  Use together
    with ``app.setStyle("Fusion")``."""
    from PySide6.QtGui import QColor, QPalette

    def c(h):
        return QColor(h)

    p = QPalette()
    p.setColor(QPalette.Window, c(COLORS["bg"]))
    p.setColor(QPalette.WindowText, c(COLORS["text"]))
    p.setColor(QPalette.Base, c(COLORS["panel_hi"]))
    p.setColor(QPalette.AlternateBase, c(COLORS["panel"]))
    p.setColor(QPalette.Text, c(COLORS["text"]))
    p.setColor(QPalette.Button, c(COLORS["panel_hi"]))
    p.setColor(QPalette.ButtonText, c(COLORS["text"]))
    p.setColor(QPalette.ToolTipBase, c(COLORS["panel"]))
    p.setColor(QPalette.ToolTipText, c(COLORS["text"]))
    p.setColor(QPalette.PlaceholderText, c(COLORS["muted"]))
    p.setColor(QPalette.Highlight, c(COLORS["accent"]))
    p.setColor(QPalette.HighlightedText, c("#201400"))
    p.setColor(QPalette.Disabled, QPalette.Text, c(COLORS["muted"]))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, c(COLORS["muted"]))
    app.setPalette(p)


# keep the old name working for any caller that imported it
apply_dark_palette = apply_palette


# ---- style sheet ---------------------------------------------------------- #
def build_stylesheet() -> str:
    """Return the Qt style sheet for the ACTIVE palette (call after set_theme)."""
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
QLabel#hint {{ color: {c['muted']}; font-size: 11px; }}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background: {c['panel_hi']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 4px 6px;
    selection-background-color: {c['accent']};
    selection-color: #101010;
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {c['accent']};
}}
QComboBox QAbstractItemView {{
    background: {c['panel_hi']};
    border: 1px solid {c['border']};
    selection-background-color: {c['pressed']};
}}
QPushButton {{
    background: {c['panel_hi']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 12px;
}}
QPushButton:hover {{ background: {c['border']}; }}
QPushButton:pressed {{ background: {c['pressed']}; }}
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
QPushButton#danger:hover {{ background: {c['accent_dim']}; }}
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


# back-compat: some callers imported STYLESHEET as a constant.  It reflects the
# palette that was active at import time; prefer build_stylesheet() at runtime.
STYLESHEET = build_stylesheet()


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
