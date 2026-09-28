"""Theme: dark and light palettes with an amber accent.

Everything visual sources its colors from the module-level ``COLORS`` dict.
``set_theme("dark"|"light")`` swaps the palette IN PLACE (it mutates the same
dict object), so modules that did ``from .theme import COLORS`` keep seeing the
active values. Call ``set_theme`` once at startup, before building widgets.

``build_stylesheet()`` returns the Qt style sheet for the active palette, and
``apply_palette(app)`` sets a matching QPalette (use with the Fusion style so
even scroll-area viewports and popups follow the theme).
"""
from pathlib import Path

# ---- the two palettes ------------------------------------------------------

DARK = {
    "bg":        "#0e1013",
    "panel":     "#171a1f",
    "panel_hi":  "#1e222a",
    "border":    "#2a2f37",
    "text":      "#e8eaed",
    "muted":     "#8b929c",
    "accent":    "#ff9e2c",
    "accent_hi": "#ffb454",
    "accent_dim":"#a8641a",
    "ok":        "#3ddc84",
    "danger":    "#ff5c5c",
    "grid":      "#20242b",
    "pressed":   "#402a12",
    "code_bg":   "#0a0c0f",
}

LIGHT = {
    "bg":        "#f3f4f6",
    "panel":     "#ffffff",
    "panel_hi":  "#eceef1",
    "border":    "#d3d7de",
    "text":      "#1b1e24",
    "muted":     "#6b7280",
    "accent":    "#d9821a",
    "accent_hi": "#c26a12",
    "accent_dim":"#a05e12",
    "ok":        "#1a9e57",
    "danger":    "#d13b3b",
    "grid":      "#e3e6ea",
    "pressed":   "#e6d7bd",
    "code_bg":   "#f0f1f3",
}

THEMES = {"dark": DARK, "light": LIGHT}

# active palette (starts dark); mutated in place by set_theme so importers follow
COLORS = dict(DARK)


def set_theme(name: str) -> None:
    COLORS.clear()
    COLORS.update(THEMES.get((name or "dark").lower(), DARK))


# ---- Qt palette ------------------------------------------------------------

def apply_palette(app) -> None:
    from PySide6.QtGui import QPalette, QColor

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
    app.setPalette(p)


# keep the old name working
apply_dark_palette = apply_palette


# ---- style sheet -----------------------------------------------------------

def build_stylesheet() -> str:
    c = COLORS
    return f"""
* {{
    font-family: "Segoe UI", "Inter", "DejaVu Sans", sans-serif;
    font-size: 13px;
    color: {c['text']};
}}
QMainWindow, QWidget#root {{
    background: {c['bg']};
}}
QFrame#card {{
    background: {c['panel']};
    border: 1px solid {c['border']};
    border-radius: 12px;
}}
QLabel#cardTitle {{
    color: {c['muted']};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel#bigValue {{
    font-size: 34px;
    font-weight: 700;
    color: {c['text']};
}}
QLabel#unit {{ color: {c['muted']}; font-size: 14px; }}
QLabel#stateBadge {{
    background: {c['panel_hi']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 4px 12px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QPushButton {{
    background: {c['panel_hi']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 600;
}}
QPushButton:hover {{ border-color: {c['accent']}; }}
QPushButton:pressed {{ background: {c['pressed']}; }}
QPushButton#primary {{ background: {c['accent']}; color: #201400; border: none; }}
QPushButton#primary:hover {{ background: {c['accent_hi']}; }}
QPushButton#danger:hover {{ border-color: {c['danger']}; }}
QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox {{
    background: {c['panel_hi']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: {c['accent']};
}}
QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus, QComboBox:focus {{
    border-color: {c['accent']};
}}
QComboBox QAbstractItemView {{
    background: {c['panel_hi']};
    border: 1px solid {c['border']};
    selection-background-color: {c['pressed']};
}}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border: 1px solid {c['border']};
    border-radius: 5px;
    background: {c['panel_hi']};
}}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
QPlainTextEdit#log {{
    background: {c['code_bg']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    font-family: "Cascadia Code", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 12px;
}}
QLabel#sectionLabel {{
    color: {c['muted']};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QDialog {{ background: {c['bg']}; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{
    background: transparent;
    color: {c['muted']};
    padding: 8px 16px;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 600;
}}
QTabBar::tab:selected {{
    color: {c['accent']};
    border-color: {c['border']};
    border-bottom-color: {c['bg']};
}}
QTabBar::tab:hover:!selected {{ color: {c['text']}; }}
QLabel#hint {{ color: {c['muted']}; font-size: 11px; }}
"""


# back-compat: some callers imported STYLESHEET as a constant
STYLESHEET = build_stylesheet()


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
