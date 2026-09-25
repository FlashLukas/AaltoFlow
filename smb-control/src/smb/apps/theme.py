"""Theme (dark / light) for the GUI.

All GUI colours come from ONE module-level `COLORS` dict. Other modules do
`from .theme import COLORS`, so the active palette must be swapped by MUTATING
this dict in place (`COLORS.clear(); COLORS.update(...)`), never by rebinding it
-- otherwise those imports would keep pointing at the old object.

`set_theme(name)` is called once at start-up, before any widget is built, so
every widget (and every `QPainter` that reads `COLORS`) picks up the right
palette automatically. The theme is a start-up setting only -- there is no live
toggle.

The neutral colours are identical across all the lab modules so the windows
match; only the accent differs per instrument (this module keeps amber for
identity, using a slightly deeper amber in light mode so it stays legible on
white).
"""
from pathlib import Path

# ---- palettes --------------------------------------------------------------

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
    # accent = a deeper amber so it stays legible on white:
    "accent": "#d9821a", "accent_hi": "#c26a12", "accent_dim": "#a05e12",
}

THEMES = {"dark": DARK, "light": LIGHT}

# The ACTIVE palette. Starts as dark; mutated in place by set_theme(). Import
# this dict elsewhere and read COLORS["..."] at use time to follow the theme.
COLORS = dict(DARK)


def set_theme(name: str) -> None:
    """Swap the active palette IN PLACE (so `from .theme import COLORS` keeps
    seeing the live values). Unknown / empty name falls back to dark."""
    COLORS.clear()
    COLORS.update(THEMES.get((name or "dark").lower(), DARK))


# ---- Qt palette (Fusion) ---------------------------------------------------

def apply_palette(app) -> None:
    """Set a QPalette from the ACTIVE COLORS so widgets the stylesheet doesn't
    reach (scroll-area viewports, popups, native dialogs) follow the theme too.
    Use together with `app.setStyle("Fusion")`."""
    from PySide6.QtGui import QPalette, QColor

    def c(key):
        return QColor(COLORS[key])

    p = QPalette()
    p.setColor(QPalette.Window, c("bg"))
    p.setColor(QPalette.WindowText, c("text"))
    p.setColor(QPalette.Base, c("panel_hi"))
    p.setColor(QPalette.AlternateBase, c("panel"))
    p.setColor(QPalette.Text, c("text"))
    p.setColor(QPalette.Button, c("panel_hi"))
    p.setColor(QPalette.ButtonText, c("text"))
    p.setColor(QPalette.ToolTipBase, c("panel"))
    p.setColor(QPalette.ToolTipText, c("text"))
    p.setColor(QPalette.PlaceholderText, c("muted"))
    p.setColor(QPalette.Highlight, c("accent"))
    p.setColor(QPalette.HighlightedText, QColor("#201400"))
    p.setColor(QPalette.Disabled, QPalette.Text, c("muted"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, c("muted"))
    app.setPalette(p)


# ---- stylesheet (built from the ACTIVE palette) ----------------------------

def build_stylesheet() -> str:
    """Return the Qt stylesheet (QSS) built from the ACTIVE COLORS. Call this
    AFTER set_theme() so it reflects the chosen palette."""
    return f"""
* {{
    font-family: "Segoe UI", "Inter", "DejaVu Sans", sans-serif;
    font-size: 13px;
    color: {COLORS['text']};
}}
QMainWindow, QWidget#root {{
    background: {COLORS['bg']};
}}
QFrame#card {{
    background: {COLORS['panel']};
    border: 1px solid {COLORS['border']};
    border-radius: 12px;
}}
QLabel#cardTitle {{
    color: {COLORS['muted']};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel#bigValue {{
    font-size: 34px;
    font-weight: 700;
    color: {COLORS['text']};
}}
QLabel#unit {{
    color: {COLORS['muted']};
    font-size: 14px;
}}
QLabel#stateBadge {{
    background: {COLORS['panel_hi']};
    border: 1px solid {COLORS['border']};
    border-radius: 10px;
    padding: 4px 12px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QPushButton {{
    background: {COLORS['panel_hi']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 600;
}}
QPushButton:hover {{ border-color: {COLORS['accent']}; }}
QPushButton:pressed {{ background: {COLORS['pressed']}; }}
QPushButton#primary {{
    background: {COLORS['accent']};
    color: #201400;
    border: none;
}}
QPushButton#primary:hover {{ background: {COLORS['accent_hi']}; }}
QPushButton#danger {{
    background: {COLORS['danger']};
    color: #2a0000;
    border: none;
}}
QPushButton#danger:hover {{ background: {COLORS['accent_hi']}; }}
QDoubleSpinBox, QSpinBox {{
    background: {COLORS['panel_hi']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: {COLORS['accent']};
}}
QDoubleSpinBox:focus, QSpinBox:focus {{ border-color: {COLORS['accent']}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border: 1px solid {COLORS['border']};
    border-radius: 5px;
    background: {COLORS['panel_hi']};
}}
QCheckBox::indicator:checked {{
    background: {COLORS['accent']};
    border-color: {COLORS['accent']};
}}
QPlainTextEdit#log {{
    background: {COLORS['code_bg']};
    border: 1px solid {COLORS['border']};
    border-radius: 10px;
    font-family: "Cascadia Code", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 12px;
}}
QLabel#sectionLabel {{
    color: {COLORS['muted']};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
}}

/* ---- Settings dialog ---- */
QDialog {{ background: {COLORS['bg']}; }}
QLineEdit, QComboBox {{
    background: {COLORS['panel_hi']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: {COLORS['accent']};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {COLORS['accent']}; }}
QComboBox QAbstractItemView {{
    background: {COLORS['panel_hi']};
    border: 1px solid {COLORS['border']};
    selection-background-color: {COLORS['accent_dim']};
}}
QTabWidget::pane {{
    border: 1px solid {COLORS['border']};
    border-radius: 10px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {COLORS['muted']};
    padding: 8px 16px;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 600;
}}
QTabBar::tab:selected {{
    color: {COLORS['accent']};
    border-color: {COLORS['border']};
    border-bottom-color: {COLORS['bg']};
}}
QTabBar::tab:hover:!selected {{ color: {COLORS['text']}; }}
QLabel#hint {{ color: {COLORS['muted']}; font-size: 11px; }}
"""


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
