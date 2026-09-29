"""The GUI side of control (see control.py): a bar + an input guard.

A GUI connected to a service (``--connect``) puts a ``ControlBar`` at the top of
its window. The bar shows who holds control, and:

* in CONTROL: "You have control" (+ who else is watching) and a Release button;
* as a VIEWER: "VIEWER -- <who> has control" and a Take control button. Every
  input of the main window (buttons, number boxes, combos, text fields,
  sliders) swallows mouse, wheel and key input, and a blocked click says why.
  Readouts, the live picture, tabs and scrolling keep working. Widgets marked
  with ``mark_always(widget)`` stay usable -- STOP, Kill AF: the service lets
  anyone send those, so the GUI must not hide them.

Why block input here AND refuse in the service: the service is what protects
the instrument (control.py); this guard is what makes a viewer window honest
-- without it a click would change a box locally, then fail with an error.
Why an event filter and not setEnabled(False): every GUI enables and disables
its own widgets from its status timer (during an autofocus, with the stage
down, ...). Disabling from outside would fight that code, and re-enabling
afterwards would switch on buttons that ought to stay off.

Dialogs (Settings, confirmations) are NOT guarded: a viewer may look at the
settings, and an OK there is refused by the service with a clear message.

Byte-identical copies live in each module's ``src/<pkg>/apps/control_bar.py``
(master: suite-common/src/suite_common/control_bar.py; tools/check_modules.py
compares them). The client passed in must be a control.ControlClient.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox,
                               QApplication, QComboBox, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QTextEdit, QToolTip, QWidget)

#: Qt property that exempts a widget from the viewer guard (STOP, Kill AF, ...)
ALWAYS_PROPERTY = "control_always"

_INPUT_TYPES = (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit,
                QAbstractSlider, QTextEdit, QPlainTextEdit)
_BLOCKED_EVENTS = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                   QEvent.Type.MouseButtonDblClick, QEvent.Type.KeyPress,
                   QEvent.Type.KeyRelease, QEvent.Type.Wheel}

# Fixed colours on purpose (not the module's theme): the bar must read the same
# in every module and both themes -- dark text on amber is legible on either.
# (The label's background must be transparent: a module stylesheet may give
# every QLabel the panel colour, which in the dark theme put dark text on a
# dark patch in the middle of the amber bar.)
_VIEWER_STYLE = ("QFrame#controlBar { background: #ffb454; border-radius: 4px; }"
                 "QFrame#controlBar QLabel { color: #1a1a1a; background: transparent;"
                 " font-weight: bold; }")
_FLASH_STYLE = ("QFrame#controlBar { background: #ff5c5c; border-radius: 4px; }"
                "QFrame#controlBar QLabel { color: #ffffff; background: transparent;"
                " font-weight: bold; }")
_CONTROL_STYLE = "QFrame#controlBar { background: transparent; }"


def mark_always(*widgets: QWidget) -> None:
    """Keep these widgets usable in viewer mode (safety: STOP, Kill AF)."""
    for w in widgets:
        if w is not None:
            w.setProperty(ALWAYS_PROPERTY, True)


def _who(h: dict | None) -> str:
    if not h:
        return "nobody"
    return f"{h.get('name') or h.get('kind') or 'a client'} ({h.get('host') or '?'})"


class _Guard(QObject):
    """Application-wide event filter: swallows input to the window's inputs."""

    def __init__(self, bar: "ControlBar"):
        super().__init__(bar)
        self.bar = bar

    def eventFilter(self, obj, ev) -> bool:           # noqa: N802 (Qt name)
        if not self.bar.viewer or ev.type() not in _BLOCKED_EVENTS:
            return False
        if not isinstance(obj, QWidget):
            return False
        w = self.bar.guarded_input(obj)
        if w is None:
            return False
        if ev.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress):
            self.bar.flash()
        return True


class ControlBar(QFrame):
    """One line at the top of a remote GUI: control or viewer, and the switch.

    ``window`` = the main window whose inputs are guarded; ``log(level, msg)``
    receives what the bar has to say (optional). Call ``refresh()`` from the
    GUI's status timer, and ``claim_if_free()`` once after the client started:
    the first GUI to connect gets control, later ones open as viewers.
    """

    def __init__(self, client, window: QWidget, log=None, parent=None):
        super().__init__(parent or window)
        self.setObjectName("controlBar")
        self.client = client
        self.window_ = window
        self.log = log or (lambda level, msg: None)
        self.viewer = False
        self._flashing = False
        self._last_key = None

        self.label = QLabel("")
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.label.setWordWrap(True)     # a narrow window: two lines, not a cut
        self.btn_take = QPushButton("Take control")
        self.btn_take.setToolTip("Take control of this instrument. If someone else "
                                 "has it, you are asked first; they become a viewer.")
        self.btn_release = QPushButton("Release")
        self.btn_release.setToolTip("Give up control: this window becomes a viewer "
                                    "and anyone may take control.")
        mark_always(self.btn_take, self.btn_release)
        self.btn_take.clicked.connect(self._take)
        self.btn_release.clicked.connect(self._release)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.btn_take)
        lay.addWidget(self.btn_release)

        self._guard = _Guard(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._guard)
        self.hide()                      # until the service says it knows control

    # -- which widgets are guarded ---------------------------------------- #
    def guarded_input(self, obj: QWidget) -> QWidget | None:
        """The input widget ``obj`` belongs to, if the viewer guard covers it.

        Walks up from the widget that got the event (a spin box's inner line
        edit, a combo's view) to the first input type, within the main window
        only (dialogs are separate windows and pass).
        """
        if obj.window() is not self.window_:
            return None
        w = obj
        while w is not None and w is not self.window_:
            if w.property(ALWAYS_PROPERTY):
                return None
            if w is self:
                return None
            if isinstance(w, _INPUT_TYPES):
                if isinstance(w, (QTextEdit, QPlainTextEdit)) and w.isReadOnly():
                    return None          # a log pane: selecting text is fine
                if isinstance(w, QLineEdit) and w.isReadOnly():
                    return None
                return w
            w = w.parentWidget()
        return None

    # -- state ---------------------------------------------------------------- #
    def claim_if_free(self) -> None:
        try:
            if self.client.take_control(force=False):
                self.log("info", "control: this window has control")
            else:
                h = (self.client.control() or {}).get("holder")
                self.log("info", f"control: {_who(h)} has control -- this window is a viewer")
        except Exception as exc:
            # an older service without control: behave as before (full control)
            self.log("info", f"control: not supported by this service ({exc})")
        self.refresh()

    def refresh(self) -> None:
        ctl = self.client.control()
        if ctl is None:                  # service without control: no bar, no guard
            self.viewer = False
            self.hide()
            return
        self.show()
        holder = ctl.get("holder")
        mine = self.client.has_control()
        self.viewer = not mine
        others = [c for c in ctl.get("clients", [])
                  if c.get("id") != self.client.identity["id"]
                  and c.get("id") != (holder or {}).get("id")]
        key = (mine, (holder or {}).get("id"), tuple(sorted(c.get("id", "") for c in others)))
        if key == self._last_key and not self._flashing:
            return
        self._last_key = key
        watching = ", ".join(_who(c) for c in others)
        if mine:
            text = "You have control"
            if watching:
                text += f"  ·  also connected: {watching}"
        elif holder:
            since = time.strftime("%H:%M", time.localtime(holder.get("since", 0)))
            text = (f"VIEWER — {_who(holder)} has control since {since}; "
                    "nothing can be changed here")
        else:
            text = "VIEWER — nobody has control at the moment."
        self.label.setText(text)
        self.btn_take.setVisible(not mine)
        self.btn_release.setVisible(mine)
        if not self._flashing:
            self.setStyleSheet(_CONTROL_STYLE if mine else _VIEWER_STYLE)

    def flash(self) -> None:
        """A blocked click: say why, briefly, where the user is looking."""
        QToolTip.showText(QCursor.pos(), "Viewer: take control to change values")
        if self._flashing:
            return
        self._flashing = True
        self.setStyleSheet(_FLASH_STYLE)

        def _back():
            self._flashing = False
            self._last_key = None
            self.refresh()
        QTimer.singleShot(600, _back)

    # -- buttons ---------------------------------------------------------- #
    def _take(self) -> None:
        holder = (self.client.control() or {}).get("holder")
        force = False
        if holder and not self.client.has_control():
            since = time.strftime("%H:%M", time.localtime(holder.get("since", 0)))
            ans = QMessageBox.question(
                self.window_, "Take control",
                f"{_who(holder)} has control since {since}.\n\n"
                "Take it over? They become a viewer (they see that you took it) "
                "and can take it back the same way.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
            force = True
        try:
            ok = self.client.take_control(force=force)
            self.log("info" if ok else "warn",
                     "control: this window has control" if ok else
                     "control: someone else took it first -- try again")
        except Exception as exc:
            self.log("error", f"control: {exc}")
        self._last_key = None
        self.refresh()

    def _release(self) -> None:
        try:
            self.client.release_control()
            self.log("info", "control: released -- this window is a viewer")
        except Exception as exc:
            self.log("error", f"control: {exc}")
        self._last_key = None
        self.refresh()
