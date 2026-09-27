"""The front panel: MainWindow + the signature MountIndicator.

PySide6, Fusion style, dark/light theme.  ``run_app(ctrl, cfg, remote=False)``
builds the window; ``ctrl`` is either a local :class:`RotationMount` brain or an
:class:`ElliptecClient` -- they share a method surface, so the GUI does not care.

Layout: a top bar (title, Settings..., STOP ALL), then ONE CARD PER MOUNT (one
per configured bus address): the indicator dial on the left, the angle readout
and the controls on the right.  A log at the bottom.

Threading rule: instrument events arrive on a background thread, so they MUST
cross into Qt through a signal -- see :class:`Bridge`.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QLocale, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from . import theme
from .settings_dialog import SettingsDialog
from .theme import repolish


# --------------------------------------------------------------------------- #
# event bridge (thread boundary -> Qt)
# --------------------------------------------------------------------------- #
class Bridge(QObject):
    event = Signal(str, str)  # (level, message)


# --------------------------------------------------------------------------- #
# small UI helpers
# --------------------------------------------------------------------------- #
def _c_locale() -> QLocale:
    """C locale without group separators: on a Finnish/Czech Windows a Qt
    spin box would otherwise show 1,5 or 1 000 (gotcha #18)."""
    loc = QLocale.c()
    loc.setNumberOptions(QLocale.OmitGroupSeparator)
    return loc


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(8)
    cap = QLabel(title)
    cap.setObjectName("cardTitle")
    lay.addWidget(cap)
    return frame, lay


def _dspin(value=0.0, lo=-1e6, hi=1e6, step=1.0, decimals=3) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setLocale(_c_locale())
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    s.setMinimumWidth(80)
    return s


def _fmt(v, nd=3) -> str:
    return "--" if v is None else f"{v:.{nd}f}"


# --------------------------------------------------------------------------- #
# signature indicator widget
# --------------------------------------------------------------------------- #
class MountIndicator(QWidget):
    """The rotation mount seen face-on, looking along the beam.

    * the knurled housing ring with a degree scale (0 deg at 3 o'clock,
      angles growing counter-clockwise -- the usual optics convention);
    * the optic in the aperture, with its AXIS drawn through the centre at
      the current user angle (think: the fast axis of a waveplate or the
      transmission axis of a polariser) -- the line is drawn both ways, since
      an axis at 10 deg and at 190 deg is the same orientation of the optic;
    * a triangle on the rim at the TARGET angle;
    * while turning, an arc from the angle to the target and a pulsing ring
      (own ~33 ms timer, independent of the status poll);
    * the home mark (device 0 deg) as a dot: green once homed, red before.
    Colours are read from theme.COLORS at paint time (never cached).
    """

    def __init__(self):
        super().__init__()
        self.setMinimumSize(200, 200)
        self._angle = None
        self._target = None
        self._offset = 0.0
        self._moving = False
        self._homed = False
        self._error = ""
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, angle, target, offset, moving, homed, error="") -> None:
        self._angle = angle
        self._target = target
        self._offset = offset or 0.0
        self._moving = bool(moving)
        self._homed = bool(homed)
        self._error = error or ""
        if self._moving and not self._timer.isActive():
            self._timer.start()
        elif not self._moving and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.15) % (2 * math.pi)
        self.update()

    @staticmethod
    def _pt(cx, cy, r, deg) -> QPointF:
        # Screen y grows downwards, so a counter-clockwise angle subtracts.
        a = math.radians(deg)
        return QPointF(cx + r * math.cos(a), cy - r * math.sin(a))

    def paintEvent(self, _event) -> None:
        C = theme.COLORS
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - 8
        cx, cy = self.width() / 2, self.height() / 2
        R = side / 2                 # outer edge of the housing
        r_ap = R * 0.62              # the aperture / optic

        # -- housing ring with knurling ---------------------------------- #
        p.setPen(QPen(QColor(C["border"]), 1.5))
        p.setBrush(QColor(C["panel_hi"]))
        p.drawEllipse(QPointF(cx, cy), R, R)
        p.setPen(QPen(QColor(C["grid"]), 1))
        for k in range(72):          # knurl every 5 deg, turning with the optic
            base = (self._angle or 0.0) + 2.5
            a = base + k * 5
            p.drawLine(self._pt(cx, cy, R - 1, a), self._pt(cx, cy, R * 0.93, a))

        # degree scale (fixed: it is the lab frame, not part of the rotor)
        p.setPen(QPen(QColor(C["muted"]), 1))
        f = QFont(p.font())
        f.setPointSizeF(max(6.5, side / 34))
        p.setFont(f)
        for d in range(0, 360, 30):
            inner = R * (0.80 if d % 90 == 0 else 0.84)
            p.drawLine(self._pt(cx, cy, R * 0.88, d), self._pt(cx, cy, inner, d))
        for d in (0, 90, 180, 270):
            pt = self._pt(cx, cy, R * 0.71, d)
            p.drawText(QRectF(pt.x() - 16, pt.y() - 8, 32, 16), Qt.AlignCenter, str(d))

        # -- the optic ------------------------------------------------------ #
        glass = QColor(C["accent"])
        glass.setAlpha(28 if not self._moving else 45)
        p.setPen(QPen(QColor(C["border"]), 1.2))
        p.setBrush(QColor(C["code_bg"]))
        p.drawEllipse(QPointF(cx, cy), r_ap, r_ap)
        p.setBrush(glass)
        p.drawEllipse(QPointF(cx, cy), r_ap, r_ap)

        # travelling arc: from where it is to where it is going
        if self._moving and self._angle is not None and self._target is not None:
            span = ((self._target - self._angle + 180.0) % 360.0) - 180.0
            arc = QColor(C["accent"])
            arc.setAlpha(150)
            p.setPen(QPen(arc, max(3.0, side / 45), Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            rr = R * 0.97 - side / 90
            p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr),
                      int(self._angle * 16), int(span * 16))
            halo = QColor(C["accent_hi"])
            halo.setAlpha(int(60 + 60 * (0.5 + 0.5 * math.sin(self._phase))))
            p.setPen(QPen(halo, 2))
            p.drawEllipse(QPointF(cx, cy), r_ap + 3, r_ap + 3)

        # the optic's axis: a double-ended line through the centre
        if self._angle is not None:
            ax_col = QColor(C["accent_hi"] if self._moving else C["accent"])
            p.setPen(QPen(ax_col, max(2.5, side / 60), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(self._pt(cx, cy, r_ap * 0.92, self._angle),
                       self._pt(cx, cy, r_ap * 0.92, self._angle + 180))
            # the perpendicular (slow axis / extinction direction), faint
            p.setPen(QPen(QColor(C["muted"]), 1, Qt.DashLine))
            p.drawLine(self._pt(cx, cy, r_ap * 0.6, self._angle + 90),
                       self._pt(cx, cy, r_ap * 0.6, self._angle + 270))
            # an arrow head on the "0 deg" end, so 10 and 190 differ visibly
            tip = self._pt(cx, cy, r_ap * 0.92, self._angle)
            l = self._pt(cx, cy, r_ap * 0.74, self._angle + 7)
            rgt = self._pt(cx, cy, r_ap * 0.74, self._angle - 7)
            p.setPen(Qt.NoPen)
            p.setBrush(ax_col)
            p.drawPolygon(QPolygonF([tip, l, rgt]))
        p.setBrush(QColor(C["text"]))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), 3, 3)

        # -- target marker on the rim ------------------------------------ #
        if self._target is not None:
            t = self._target
            tip = self._pt(cx, cy, R * 0.90, t)
            a1 = self._pt(cx, cy, R * 1.0, t + 4)
            a2 = self._pt(cx, cy, R * 1.0, t - 4)
            p.setBrush(QColor(C["accent_hi"]))
            p.setPen(QPen(QColor(C["bg"]), 1))
            p.drawPolygon(QPolygonF([tip, a1, a2]))

        # -- home mark (device 0 deg, i.e. user angle -offset) ----------- #
        hm = self._pt(cx, cy, R * 0.775, -self._offset)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(C["ok"] if self._homed else C["danger"]))
        p.drawEllipse(hm, 3.5, 3.5)

        # -- an error paints the housing edge red ------------------------- #
        if self._error:
            path = QPainterPath()
            path.addEllipse(QPointF(cx, cy), R - 1, R - 1)
            p.setPen(QPen(QColor(C["danger"]), 3))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        p.end()


# --------------------------------------------------------------------------- #
# one card per mount
# --------------------------------------------------------------------------- #
class AxisCard(QFrame):
    def __init__(self, win: "MainWindow", axis: int, name: str, address: str):
        super().__init__()
        self.win = win
        self.axis = axis
        self.setObjectName("card")
        cfg = win.cfg
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        cap = QLabel(f"{name.upper()}   ·   ADDRESS {address}")
        cap.setObjectName("cardTitle")
        outer.addWidget(cap)

        row = QHBoxLayout()
        row.setSpacing(16)
        self.indicator = MountIndicator()
        row.addWidget(self.indicator, 0)

        right = QVBoxLayout()
        right.setSpacing(6)
        c = QLabel("ANGLE  (deg)")
        c.setObjectName("caption")
        right.addWidget(c)
        self.big = QLabel("--")
        self.big.setObjectName("bigValue")
        right.addWidget(self.big)
        self.sub = QLabel("device -- · offset -- · target --")
        self.sub.setObjectName("muted")
        right.addWidget(self.sub)
        self.state = QLabel("--")
        self.state.setObjectName("muted")
        right.addWidget(self.state)

        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        g.addWidget(QLabel("go to"), 0, 0)
        self.target = _dspin(0.0, 0.0, 360.0, cfg.motion.jog_step_deg, 3)
        g.addWidget(self.target, 0, 1)
        go = QPushButton("Go")
        go.setObjectName("primary")
        repolish(go)
        go.clicked.connect(lambda: win._do(lambda: win.ctrl.move_abs(axis, self.target.value())))
        g.addWidget(go, 0, 2, 1, 2)

        g.addWidget(QLabel("step"), 1, 0)
        self.step = _dspin(cfg.motion.jog_step_deg, 0.0, 360.0, 1.0, 3)
        g.addWidget(self.step, 1, 1)
        minus, plus = QPushButton("−"), QPushButton("+")
        minus.clicked.connect(lambda: win._do(lambda: win.ctrl.move_rel(axis, -self.step.value())))
        plus.clicked.connect(lambda: win._do(lambda: win.ctrl.move_rel(axis, self.step.value())))
        g.addWidget(minus, 1, 2)
        g.addWidget(plus, 1, 3)

        g.addWidget(QLabel("velocity %"), 2, 0)
        self.vel = QSpinBox()
        self.vel.setLocale(_c_locale())
        self.vel.setRange(1, 100)
        self.vel.setValue(int(cfg.motion.velocity_pct))
        g.addWidget(self.vel, 2, 1)
        setv = QPushButton("Set")
        setv.clicked.connect(lambda: win._do(lambda: win.ctrl.set_velocity(axis, self.vel.value())))
        g.addWidget(setv, 2, 2, 1, 2)
        right.addLayout(g)

        brow = QHBoxLayout()
        for text, fn, tip in (
            ("Home", lambda: win.ctrl.home(axis), "Turn to the home mark and re-reference"),
            ("Zero here", lambda: win.ctrl.set_zero(axis), "Call the current angle 0 deg"),
            ("Clear zero", lambda: win.ctrl.clear_zero(axis), "Back to the device frame (offset 0)"),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, f=fn: win._do(f))
            brow.addWidget(b)
        stop = QPushButton("Stop")
        stop.setObjectName("danger")
        repolish(stop)
        stop.clicked.connect(lambda: win._do(lambda: win.ctrl.stop(axis)))
        brow.addWidget(stop)
        right.addLayout(brow)
        right.addStretch(1)
        row.addLayout(right, 1)
        outer.addLayout(row)
        self._synced = False

    def refresh(self, st) -> None:
        i = self.axis
        if not self._synced and i < len(st.velocity_pct or []):
            # Start the input boxes from what the mount is doing, once: a
            # remote panel opened on a running service must not offer 100 %
            # when the mount runs at 30 %.  After that they are the user's.
            self._synced = True
            if st.velocity_pct[i] is not None:     # None = the mount's speed is unknown
                self.vel.setValue(int(st.velocity_pct[i]))
            if st.target_deg[i] is not None:
                self.target.setValue(float(st.target_deg[i]))

        def at(lst, default=None):
            return lst[i] if lst is not None and i < len(lst) else default

        ang, dev, tgt = at(st.angle_deg), at(st.device_deg), at(st.target_deg)
        off = at(st.offset_deg, 0.0)
        moving, homed, err = at(st.moving, False), at(st.homed, False), at(st.error, "")
        self.big.setText(_fmt(ang))
        self.sub.setText(f"device {_fmt(dev)} · offset {_fmt(off)} · target {_fmt(tgt)}")
        parts = ["MOVING" if moving else "idle", "homed" if homed else "not homed",
                 f"{'--' if at(st.velocity_pct) is None else at(st.velocity_pct)} %"]
        if err:
            parts.append(f"ERROR: {err}")
        self.state.setText("  ·  ".join(parts))
        self.indicator.set_state(ang, tgt, off, moving, homed, err)


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class MainWindow(QWidget):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self.remote = remote
        self.setWindowTitle("Elliptec rotation mount" + ("  [remote]" if remote else ""))

        self._bridge = Bridge()
        self._bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda level, msg: self._bridge.event.emit(level, msg)

        self._build_ui()
        n = len(self._cards)
        self.resize(860, min(980, 330 + 300 * n))

        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("ELLIPTEC ROTATION MOUNT")
        title.setObjectName("cardTitle")
        top.addWidget(title)
        top.addStretch(1)
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        top.addWidget(settings_btn)
        stop_all = QPushButton("STOP ALL")
        stop_all.setObjectName("danger")
        repolish(stop_all)
        stop_all.clicked.connect(lambda: self._do(self.ctrl.stop_all))
        top.addWidget(stop_all)
        root.addLayout(top)

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        self._cards = []
        for i, (addr, name) in enumerate(zip(self.ctrl.addresses, self.ctrl.names)):
            card = AxisCard(self, i, name, addr)
            self._cards.append(card)
            col.addWidget(card)
        col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        root.addWidget(scroll, 3)

        frame, lay = _card("LOG")
        self._log = QPlainTextEdit()
        self._log.setObjectName("log")
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        self._log.setMinimumHeight(60)
        self._log.setMaximumHeight(80)
        lay.addWidget(self._log)
        root.addWidget(frame, 0)

    # -- settings ------------------------------------------------------- #
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self._push_config()
            self._on_event("info", "settings applied")

    def _push_config(self) -> None:
        """Apply the edited config to the running controller (local or remote)."""
        if self.remote:
            from ..net.protocol import config_to_dict
            self._do(lambda: self.ctrl.set_config(config_to_dict(self.cfg)))
        else:
            self._do(self.ctrl.apply_config)

    # -- actions -------------------------------------------------------- #
    def _do(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self._on_event("error", f"{type(exc).__name__}: {exc}")

    # -- polling + events ----------------------------------------------- #
    def _refresh(self) -> None:
        st = self.ctrl.status()
        for card in self._cards:
            card.refresh(st)

    def _on_event(self, level: str, msg: str) -> None:
        C = theme.COLORS
        color = {"info": C["muted"], "warn": C["accent_hi"], "error": C["danger"]}.get(level, C["text"])
        self._log.appendHtml(f'<span style="color:{color}">[{level}]</span> {msg}')


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run_app(ctrl, cfg: Config, remote: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    # Choose the palette BEFORE any widget is built, so every widget and custom
    # paintEvent reads the right COLORS.  Startup-only: there is no live toggle.
    theme.set_theme(getattr(cfg.ui, "theme", "dark"))
    QLocale.setDefault(_c_locale())

    app = QApplication.instance() or QApplication([])
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    theme.apply_palette(app)
    app.setStyleSheet(theme.build_stylesheet())

    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """A posed simulator panel -- what tools/render_all.py calls for the README.

    Two simulated mounts on one bus (a half-wave plate and a polariser): the
    first is homed and parked at 22.5 deg; the second starts a long,
    slow turn just before the screenshot is taken, so the picture shows the
    indicator mid-move.  For normal use start scripts/run_gui.py instead.
    """
    from PySide6.QtCore import QTimer

    from ..sim_system import build_sim_system

    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    cfg.axes.addresses = "0,1"
    cfg.axes.names = "Half-wave plate,Polariser"
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain.home(0)
    # queued after the home has finished: a second motion command to a mount
    # that is still turning replaces the first one
    QTimer.singleShot(800, lambda: brain.move_abs(0, 22.5))
    brain.set_velocity(1, 30)
    QTimer.singleShot(2900, lambda: brain.move_abs(1, 300.0))
    try:
        return run_app(brain, cfg, remote=False)
    finally:
        brain.shutdown()
