"""Theme for the GUI -- dark or light, chosen once at startup.

The whole app is styled from ONE module-level ``COLORS`` dict.  Other modules do
``from .theme import COLORS`` and read it live, so :func:`set_theme` swaps the
palette by MUTATING ``COLORS`` in place (clear + update) -- it never rebinds the
name, which would leave those importers pointing at the old dict.

Startup wiring (see gui.run_app):
    set_theme(cfg.ui.theme)          # BEFORE any widget is built
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())

Identity: this module keeps its amber accent in both palettes (a slightly deeper
amber in light mode so it stays legible on white).  The neutral colours are the
shared suite values so every module matches.
"""

from __future__ import annotations
from pathlib import Path

# --------------------------------------------------------------------------- #
# Palettes (neutral colours shared across the suite; accent = this module's)
# --------------------------------------------------------------------------- #
DARK = {
    "bg": "#0e1013", "panel": "#171a1f", "panel_hi": "#1e222a", "border": "#2a2f37",
    "text": "#e8eaed", "muted": "#8b929c", "ok": "#3ddc84", "danger": "#ff5c5c",
    "grid": "#20242b", "pressed": "#402a12", "code_bg": "#0a0c0f",
    # accent = this module's dark accent (amber):
    "accent": "#ff9e2c", "accent_hi": "#ffb454", "accent_dim": "#a8641a",
}
LIGHT = {
    "bg": "#f3f4f6", "panel": "#ffffff", "panel_hi": "#eceef1", "border": "#d3d7de",
    "text": "#1b1e24", "muted": "#6b7280", "ok": "#1a9e57", "danger": "#d13b3b",
    "grid": "#e3e6ea", "pressed": "#e6d7bd", "code_bg": "#f0f1f3",
    # accent = a deeper version of the dark accent (legible on white):
    "accent": "#d9821a", "accent_hi": "#c26a12", "accent_dim": "#a05e12",
}
THEMES = {"dark": DARK, "light": LIGHT}

# The ACTIVE palette.  Mutated in place by set_theme so `from .theme import
# COLORS` importers keep seeing the current values.
COLORS = dict(DARK)


def set_theme(name: str) -> None:
    """Select the active palette by NAME ("dark"/"light"), mutating COLORS in
    place (never rebinding it)."""
    COLORS.clear()
    COLORS.update(THEMES.get((name or "dark").lower(), DARK))


def apply_palette(app) -> None:
    """Set a matching Fusion QPalette from COLORS so widgets the stylesheet
    doesn't reach (scroll-area viewports, combo popups) follow the theme too."""
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
    p.setColor(QPalette.HighlightedText, c("#101010"))
    p.setColor(QPalette.Disabled, QPalette.Text, c(COLORS["muted"]))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, c(COLORS["muted"]))
    app.setPalette(p)


def build_stylesheet() -> str:
    """Return the Qt stylesheet built from the ACTIVE COLORS palette."""
    C = COLORS
    return f"""
QWidget {{
    background: {C['bg']};
    color: {C['text']};
    font-size: 13px;
}}
QFrame#card {{
    background: {C['panel']};
    border: 1px solid {C['border']};
    border-radius: 10px;
}}
QLabel#cardTitle {{
    color: {C['muted']};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 2px;
}}
QLabel#bigValue {{
    font-size: 34px;
    font-weight: 700;
    color: {C['text']};
}}
QLabel#caption {{
    color: {C['accent_hi']};
    font-size: 11px;
}}
QLabel#muted {{ color: {C['muted']}; }}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background: {C['panel_hi']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    padding: 4px 6px;
    selection-background-color: {C['accent']};
    selection-color: #101010;
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {C['accent']};
}}
QComboBox QAbstractItemView {{
    background: {C['panel_hi']};
    border: 1px solid {C['border']};
    selection-background-color: {C['accent_dim']};
}}
QPushButton {{
    background: {C['panel_hi']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    padding: 6px 12px;
}}
QPushButton:hover {{ background: {C['border']}; }}
QPushButton:pressed {{ background: {C['pressed']}; }}
QPushButton#primary {{
    background: {C['accent']};
    color: #141414;
    border: 1px solid {C['accent']};
    font-weight: 700;
}}
QPushButton#primary:hover {{ background: {C['accent_hi']}; }}
QPushButton#danger {{
    background: {C['danger']};
    color: #141414;
    border: 1px solid {C['danger']};
    font-weight: 700;
}}
QPushButton#danger:hover {{ background: {C['accent_hi']}; }}
QCheckBox {{
    color: {C['text']};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border: 2px solid {C['muted']};
    border-radius: 4px;
    background: {C['panel_hi']};
}}
QCheckBox::indicator:hover {{
    border: 2px solid {C['accent_hi']};
}}
QCheckBox::indicator:checked {{
    background: {C['accent']};
    border: 2px solid {C['accent']};
}}
QCheckBox::indicator:checked:hover {{
    background: {C['accent_hi']};
    border: 2px solid {C['accent_hi']};
}}
QCheckBox::indicator:disabled {{
    border: 2px solid {C['border']};
    background: {C['panel']};
}}
QTableWidget {{
    background: {C['panel']};
    gridline-color: {C['grid']};
    border: 1px solid {C['border']};
    border-radius: 8px;
}}
QHeaderView::section {{
    background: {C['panel_hi']};
    color: {C['muted']};
    border: none;
    padding: 4px;
}}
QPlainTextEdit#log {{
    background: {C['code_bg']};
    border: 1px solid {C['border']};
    border-radius: 8px;
    font-family: Consolas, "DejaVu Sans Mono", monospace;
    font-size: 12px;
}}
QDialog {{ background: {C['bg']}; }}
QTabWidget::pane {{ border: 1px solid {C['border']}; border-radius: 8px; }}
QTabBar::tab {{
    background: {C['panel']};
    padding: 6px 12px;
    border: 1px solid {C['border']};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{ background: {C['panel_hi']}; color: {C['accent_hi']}; }}
QLabel#hint {{ color: {C['muted']}; font-size: 11px; }}
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
