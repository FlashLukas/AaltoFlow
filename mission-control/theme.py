"""Theme: dark and light palettes with the shared amber accent.

Same mechanism as clMag-control: all colors come from the module-level ``COLORS``
dict; ``set_theme("dark"|"light")`` swaps the palette IN PLACE (mutates the same
object) so importers keep seeing the active values. Call ``set_theme`` once at
startup before building widgets. ``build_stylesheet()`` returns the Qt style
sheet for the active palette; ``apply_palette(app)`` sets a matching QPalette
(use with the Fusion style so scroll areas and popups follow). Startup-only.
"""
from pathlib import Path

from PySide6 import QtGui, QtWidgets

# ---- the two palettes (neutral colors shared across all modules) -----------

DARK = {
    "bg": "#0e1013", "panel": "#171a1f", "panel_hi": "#1e222a", "border": "#2a2f37",
    "text": "#e8eaed", "muted": "#8b929c", "ok": "#3ddc84", "danger": "#ff5c5c",
    "grid": "#20242b", "pressed": "#402a12", "code_bg": "#0a0c0f",
    "accent": "#ff9e2c", "accent_hi": "#ffb454", "accent_dim": "#a8641a",
}
LIGHT = {
    "bg": "#f3f4f6", "panel": "#ffffff", "panel_hi": "#eceef1", "border": "#d3d7de",
    "text": "#1b1e24", "muted": "#6b7280", "ok": "#1a9e57", "danger": "#d13b3b",
    "grid": "#e3e6ea", "pressed": "#e6d7bd", "code_bg": "#f0f1f3",
    "accent": "#d9821a", "accent_hi": "#c26a12", "accent_dim": "#a05e12",
}
THEMES = {"dark": DARK, "light": LIGHT}

# active palette (starts dark); mutated in place so importers follow.
COLORS = dict(DARK)
C = COLORS          # back-compat alias — same object, follows set_theme


def set_theme(name: str) -> None:
    COLORS.clear()
    COLORS.update(THEMES.get((name or "dark").lower(), DARK))


def apply_palette(app: QtWidgets.QApplication) -> None:
    """Set a QPalette from COLORS. Use with app.setStyle('Fusion')."""
    c = COLORS
    p = QtGui.QPalette()
    p.setColor(QtGui.QPalette.Window, QtGui.QColor(c["bg"]))
    p.setColor(QtGui.QPalette.WindowText, QtGui.QColor(c["text"]))
    p.setColor(QtGui.QPalette.Base, QtGui.QColor(c["panel_hi"]))
    p.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(c["panel"]))
    p.setColor(QtGui.QPalette.Text, QtGui.QColor(c["text"]))
    p.setColor(QtGui.QPalette.Button, QtGui.QColor(c["panel_hi"]))
    p.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(c["text"]))
    p.setColor(QtGui.QPalette.ToolTipBase, QtGui.QColor(c["panel"]))
    p.setColor(QtGui.QPalette.ToolTipText, QtGui.QColor(c["text"]))
    p.setColor(QtGui.QPalette.PlaceholderText, QtGui.QColor(c["muted"]))
    p.setColor(QtGui.QPalette.Highlight, QtGui.QColor(c["accent"]))
    p.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#201400"))
    p.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor(c["muted"]))
    app.setPalette(p)


def build_stylesheet() -> str:
    c = COLORS
    return f"""
QWidget#root {{ background:{c['bg']}; }}
QLabel {{ color:{c['text']}; }}
QDialog {{ background:{c['bg']}; }}
QFrame#card {{ background:{c['panel']}; border:1px solid {c['border']}; border-radius:12px; }}
QFrame#card[active="true"] {{ background:{c['panel_hi']}; border:1px solid {c['accent']}; }}
QPushButton#profile {{ background:{c['panel_hi']}; color:{c['accent']}; border:1px solid {c['accent_dim']};
    border-radius:14px; padding:6px 14px; font-weight:700; }}
QPushButton#profile:hover {{ border-color:{c['accent']}; color:{c['accent_hi']}; }}
QLabel#title {{ color:{c['accent']}; font-size:20px; font-weight:800; letter-spacing:2px; }}
QLabel#subtitle {{ color:{c['muted']}; }}
QLabel#name {{ font-size:15px; font-weight:700; }}
QLabel#meta {{ color:{c['muted']}; font-size:11px; }}
QLabel#sectiontag {{ color:{c['muted']}; font-size:11px; font-weight:700; letter-spacing:1px; }}
QPlainTextEdit#log {{ background:{c['code_bg']}; border:1px solid {c['border']};
    border-radius:8px; color:{c['text']}; font-family:Consolas,monospace; font-size:12px; }}
QListWidget {{ background:{c['code_bg']}; border:1px solid {c['border']}; border-radius:8px; color:{c['text']}; }}
QLineEdit {{ background:{c['panel_hi']}; border:1px solid {c['border']}; border-radius:6px; padding:4px 6px; }}
QPushButton {{ background:{c['panel_hi']}; color:{c['text']}; border:1px solid {c['border']};
    border-radius:8px; padding:6px 12px; font-weight:600; }}
QPushButton:hover {{ border-color:{c['accent']}; }}
QPushButton:pressed {{ background:{c['pressed']}; }}
QPushButton:disabled {{ color:{c['muted']}; border-color:{c['border']}; background:{c['panel']}; }}
QPushButton#primary {{ background:{c['accent']}; color:#201400; border:none; }}
QPushButton#primary:hover {{ background:{c['accent_hi']}; }}
QPushButton#danger {{ background:{c['danger']}; color:#1a1a1a; border:none; }}
/* #primary / #danger are more specific than the plain :disabled rule above, so
   without these a disabled Stop still looks like a live red button. */
QPushButton#primary:disabled, QPushButton#danger:disabled {{
    color:{c['muted']}; background:{c['panel']}; border:1px solid {c['border']}; }}
QCheckBox {{ color:{c['text']}; }}
QScrollArea {{ border:none; }}
"""


def apply(app: QtWidgets.QApplication):
    """Convenience: Fusion + palette + stylesheet for the ACTIVE theme."""
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())


# ─────────────────────────── taskbar / window icon ────────────────────────
#: This module's own icon.svg -- the very drawing mission-control puts on its
#: card, so the window, the launcher and the Start menu all agree.
ICON_FILE = Path(__file__).resolve().parents[0] / "icon.svg"


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
