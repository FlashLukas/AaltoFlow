"""The front panel: MainWindow + the signature StickSlipIndicator.

PySide6, Fusion style, dark/light theme. ``run_app(ctrl, cfg, remote=False)``
builds the window; ``ctrl`` is either a local :class:`AgilisStage` brain or an
:class:`AgilisClient` -- they share a method surface, so the GUI is agnostic.

The panel is built around what makes an Agilis stage different:
  * MOVE in STEPS or MICROMETRES (a unit selector + a "relative" toggle),
  * a continuous JOG you hold down (one of the controller's four speeds),
  * the STEP AMPLITUDE per direction -- the one knob the AG-UC2 gives you --
    next to the measured STEP SIZE, which is only valid at the amplitude it was
    measured at (the panel says when it is not),
  * the LIMIT SWITCH of an AG-LS25: move to a limit (MV), let the controller
    measure / go to an absolute position (MA / PA), and measure the step size
    limit to limit in both directions.

Threading rule: instrument events arrive on a background thread, so they MUST
cross into Qt through a signal -- see :class:`Bridge`.
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import AMPLITUDE_MAX, AMPLITUDE_MIN, Config
from . import theme
from .settings_dialog import SettingsDialog
from .theme import repolish

AXES = ("X", "Y")

#: JA speeds as the operator sees them (manual, JA command).
JOG_SPEEDS = {
    1: "1 · 5 steps/s (set amplitude)",
    2: "2 · 100 steps/s (max amplitude)",
    4: "4 · 666 steps/s (set amplitude)",
    3: "3 · 1700 steps/s (max amplitude)",
}


# --------------------------------------------------------------------------- #
# event bridge (thread boundary -> Qt)
# --------------------------------------------------------------------------- #
class Bridge(QObject):
    event = Signal(str, str)  # (level, message)


# --------------------------------------------------------------------------- #
# small UI helpers
# --------------------------------------------------------------------------- #
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


def _spin(value=0.0, lo=-1e9, hi=1e9, step=1.0, decimals=3) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    s.setButtonSymbols(QDoubleSpinBox.NoButtons)
    s.setMinimumWidth(78)
    return s


def _amp_spin(value: int) -> QSpinBox:
    s = QSpinBox()
    s.setRange(AMPLITUDE_MIN, AMPLITUDE_MAX)
    s.setValue(int(value))
    s.setMinimumWidth(56)
    return s


# --------------------------------------------------------------------------- #
# signature indicator widget
# --------------------------------------------------------------------------- #
class StickSlipIndicator(QWidget):
    """XY step map + the two DRIVE WAVEFORMS of the stick-slip actuators.

    Left: the travel envelope in steps (datum at the centre, the leash box when
    armed) with the stage marker and a trail of its recent positions -- dots,
    not a line, because the stage moves in discrete steps.

    Right: one small "scope" per axis showing the sawtooth the AG-UC2 sends to
    the piezo. A slow ramp drags the stage along (stick), the steep edge snaps
    the piezo back while the stage stays (slip). The tooth HEIGHT is the step
    amplitude of the direction the axis goes (1..50), the ramp leans the way it
    moves, and it scrolls only while the axis is moving. Idle axes show both
    directions' amplitudes as a faint pair of teeth. A red LIMIT tag lights
    when the stage's limit switch is active; the step-size tag says whether the
    calibration still matches the amplitude.

    Its own ~33 ms QTimer drives the scrolling, independent of the status poll.
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setMinimumHeight(180)
        self._pos = [0, 0]
        self._moving = [False, False]
        self._dir = [1, 1]
        self._amp_f = [16, 16]
        self._amp_b = [16, 16]
        self._lo = [-1, -1]
        self._hi = [1, 1]
        self._leash = False
        self._limit = [False, False]
        self._cal_ok = [True, True]
        self._phase = [0.0, 0.0]
        self._trail: deque = deque(maxlen=40)

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, pos, moving, direction=None, amp_fwd=None, amp_bwd=None,
                  lo=None, hi=None, leash=False, limit=None, cal_ok=None) -> None:
        pos = [int(p) for p in pos]
        if not self._trail or self._trail[-1] != tuple(pos):
            self._trail.append(tuple(pos))
        self._pos = pos
        self._moving = [bool(m) for m in moving]
        for name, val in (("_amp_f", amp_fwd), ("_amp_b", amp_bwd), ("_lo", lo),
                          ("_hi", hi), ("_limit", limit), ("_cal_ok", cal_ok)):
            if val is not None:
                setattr(self, name, list(val))
        if direction is not None:
            self._dir = [d if d else self._dir[i] for i, d in enumerate(direction)]
        self._leash = bool(leash)
        if any(self._moving):
            if not self._timer.isActive():
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()
            self.update()

    def _tick(self) -> None:
        for a in range(2):
            if self._moving[a]:
                self._phase[a] = (self._phase[a] + 0.06) % 1.0
        self.update()

    def _half(self, axis: int) -> float:
        """Symmetric half-span, so the datum (step 0) sits at the CENTRE."""
        return max(abs(self._lo[axis]), abs(self._hi[axis]), 1.0)

    def _frac(self, axis: int, val: float) -> float:
        return min(1.0, max(0.0, 0.5 + val / (2.0 * self._half(axis))))

    def paintEvent(self, _event) -> None:
        C = theme.COLORS
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        m = 12
        side = min(h - 2 * m - 14, int(w * 0.45))
        mx0, my0 = m, m
        self._paint_map(p, C, mx0, my0, side)
        sx0 = mx0 + side + 18
        sw = max(60, w - sx0 - m)
        sh = (side - 10) / 2.0
        for a in range(2):
            self._paint_scope(p, C, a, sx0, my0 + a * (sh + 10), sw, sh)
        p.end()

    def _paint_map(self, p, C, x0, y0, side) -> None:
        border = QColor(C["accent"]) if self._leash else QColor(C["border"])
        p.setPen(QPen(border, 1))
        p.setBrush(QColor(C["code_bg"]))
        p.drawRoundedRect(x0, y0, side, side, 8, 8)
        p.setPen(QPen(QColor(C["grid"]), 1))
        for i in range(1, 4):
            g = side * i / 4
            p.drawLine(int(x0 + g), y0, int(x0 + g), y0 + side)
            p.drawLine(x0, int(y0 + g), x0 + side, int(y0 + g))
        cx, cy = x0 + side / 2.0, y0 + side / 2.0
        p.setPen(QPen(QColor(C["accent_dim"]), 1, Qt.DashLine))
        p.drawLine(int(cx), y0, int(cx), y0 + side)
        p.drawLine(x0, int(cy), x0 + side, int(cy))

        def to_px(sx, sy):
            return (x0 + self._frac(0, sx) * side, y0 + (1.0 - self._frac(1, sy)) * side)

        # the trail: discrete dots, older = fainter
        n = len(self._trail)
        for i, (sx, sy) in enumerate(self._trail):
            col = QColor(C["accent"])
            col.setAlpha(int(30 + 150 * (i + 1) / max(n, 1)))
            px, py = to_px(sx, sy)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(px, py), 2.0, 2.0)

        px, py = to_px(*self._pos)
        moving = any(self._moving)
        p.setPen(QPen(QColor(C["accent_hi"]), 2))
        p.setBrush(QColor(C["accent"]) if moving else QColor(C["accent_dim"]))
        # a diamond: the platen of the stage seen from above
        path = QPainterPath()
        path.moveTo(px, py - 8)
        path.lineTo(px + 8, py)
        path.lineTo(px, py + 8)
        path.lineTo(px - 8, py)
        path.closeSubpath()
        p.drawPath(path)

        p.setPen(QColor(C["accent_hi"]) if self._leash else QColor(C["muted"]))
        tag = f"LEASH ±{int(self._half(0))}" if self._leash else "travel limits"
        p.drawText(x0 + 8, y0 + 16, tag)
        p.setPen(QColor(C["muted"]))
        p.drawText(x0, y0 + side + 13, "X →   steps")
        p.save()
        p.translate(x0 - 2, y0 + side)
        p.rotate(-90)
        p.drawText(0, 0, "Y →")
        p.restore()

    def _paint_scope(self, p, C, a, x0, y0, w, h) -> None:
        p.setPen(QPen(QColor(C["border"]), 1))
        p.setBrush(QColor(C["code_bg"]))
        p.drawRoundedRect(int(x0), int(y0), int(w), int(h), 6, 6)
        base = y0 + h - 14                     # 0 V line
        full = h - 36                          # height of an amplitude-50 tooth
        p.setPen(QPen(QColor(C["grid"]), 1, Qt.DashLine))
        p.drawLine(int(x0 + 6), int(base), int(x0 + w - 6), int(base))
        p.drawLine(int(x0 + 6), int(base - full), int(x0 + w - 6), int(base - full))

        left, right = x0 + 34, x0 + w - 8
        period = 34.0
        if self._moving[a]:
            d = self._dir[a] or 1
            amp = self._amp_f[a] if d > 0 else self._amp_b[a]
            teeth = [(d, amp)]
            col = QColor(C["accent"])
            width = 2
        else:
            # idle: a faint forward tooth and a faint backward tooth
            teeth = [(1, self._amp_f[a]), (-1, self._amp_b[a])]
            col = QColor(C["muted"])
            width = 1
        path = QPainterPath()
        x = left - (self._phase[a] * period if self._moving[a] else 0.0)
        k = 0
        started = False
        while x < right:
            d, amp = teeth[k % len(teeth)]
            top = base - full * amp / AMPLITUDE_MAX
            x1, x2 = x + period * 0.85, x + period      # slow ramp, then fast edge
            # forward: ramp UP slowly, drop fast; backward: the mirror image
            ya, yb = (base, top) if d > 0 else (top, base)
            pts = [(x, ya), (x1, yb), (x2, ya)]
            for (qx, qy) in pts:
                qx = min(max(qx, left), right)
                if not started:
                    path.moveTo(qx, qy)
                    started = True
                else:
                    path.lineTo(qx, qy)
            x = x2
            k += 1
        p.save()
        p.setClipRect(int(left), int(y0 + 2), int(right - left), int(h - 4))
        p.setPen(QPen(col, width))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.restore()

        p.setPen(QColor(C["accent_hi"]) if self._moving[a] else QColor(C["muted"]))
        p.drawText(int(x0 + 8), int(y0 + 16), AXES[a])
        p.setPen(QColor(C["muted"]))
        p.drawText(int(x0 + 34), int(y0 + 16),
                   f"amp +{self._amp_f[a]} / -{self._amp_b[a]}")
        cal = "step size ok" if self._cal_ok[a] else "step size stale"
        p.setPen(QColor(C["ok"]) if self._cal_ok[a] else QColor(C["accent_hi"]))
        p.drawText(int(x0 + 150), int(y0 + 16), cal)
        if self._limit[a]:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["danger"]))
            p.drawRoundedRect(int(x0 + w - 56), int(y0 + 5), 48, 15, 4, 4)
            p.setPen(QColor(C["bg"]))
            p.drawText(int(x0 + w - 50), int(y0 + 17), "LIMIT")


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class MainWindow(QWidget):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self.remote = remote
        self.setWindowTitle("Agilis stage (AG-UC2)" + ("  [remote]" if remote else ""))
        self.resize(1280, 860)
        self._live = None                       # latest status, for conversions

        self._bridge = Bridge()
        self._bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda level, msg: self._bridge.event.emit(level, msg)

        self._build_ui()

        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()

        # while a hold-to-jog button is down, re-send the jog (dead-man keep-alive)
        self._held: dict[int, int] = {}
        self._keepalive = QTimer(self)
        self._keepalive.setInterval(400)
        self._keepalive.timeout.connect(self._keep_jogging)

        self._reload_positions()
        self._refresh()
        where = "remote service" if remote else "local brain"
        self._on_event("info", f"front panel ready ({where}); hold a JOG button to move "
                               f"continuously, it stops when you let go")
        # What start-up had to WRITE to the controller (the rule: read, change
        # nothing -- normally just MR, without which nothing can be read).
        writes = list(getattr(self._live, "startup_writes", None) or [])
        if writes:
            self._on_event("info", "start-up adopted the controller's counters and "
                                   "amplitudes; it wrote only: " + "; ".join(writes))

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("AGILIS STAGE  ·  NEWPORT AG-UC2  ·  2 AXES")
        title.setObjectName("cardTitle")
        top.addWidget(title)
        top.addStretch(1)
        self._conn = QLabel("")
        top.addWidget(self._conn)
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        top.addWidget(settings_btn)
        root.addLayout(top)

        body = QWidget()
        cols = QHBoxLayout(body)
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(12)
        right = QVBoxLayout()
        right.setSpacing(12)
        left.addWidget(self._build_readout_card())
        left.addWidget(self._build_move_card())
        left.addWidget(self._build_jog_card())
        left.addWidget(self._build_log_card(), 1)
        right.addWidget(self._build_amplitude_card())
        right.addWidget(self._build_step_size_card())
        right.addWidget(self._build_limit_card())
        right.addWidget(self._build_leash_card())
        right.addWidget(self._build_positions_card(), 1)
        cols.addLayout(left, 11)
        cols.addLayout(right, 9)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def _build_readout_card(self) -> QFrame:
        frame, lay = _card("POSITION  (µm estimate · step counter · relative)")
        self._big = []
        self._sub_lbl = []
        row = QHBoxLayout()
        for a in range(2):
            c = QVBoxLayout()
            cap = QLabel(f"{AXES[a]}  µm")
            cap.setObjectName("caption")
            big = QLabel("--")
            big.setObjectName("bigValue")
            sub = QLabel("-- steps · rel --")
            sub.setObjectName("muted")
            c.addWidget(cap)
            c.addWidget(big)
            c.addWidget(sub)
            row.addLayout(c)
            self._big.append(big)
            self._sub_lbl.append(sub)
        lay.addLayout(row)
        self._indicator = StickSlipIndicator(self.cfg)
        lay.addWidget(self._indicator)
        return frame

    def _build_move_card(self) -> QFrame:
        frame, lay = _card("MOVE  /  STEP  /  ZERO")
        mode = QHBoxLayout()
        mode.addWidget(QLabel("target unit"))
        self._unit = QComboBox()
        self._unit.addItems(["µm", "steps"])
        mode.addWidget(self._unit)
        self._rel_mode = QCheckBox("relative  (move BY the amount, from here)")
        mode.addWidget(self._rel_mode)
        mode.addStretch(1)
        lay.addLayout(mode)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self._target = []
        self._jog_buttons: list = []
        for a in range(2):
            grid.addWidget(QLabel(AXES[a]), a, 0)
            sp = _spin(0.0, -1e9, 1e9, 1.0, 3)
            grid.addWidget(sp, a, 1)
            self._target.append(sp)
            move_btn = QPushButton("Move")
            move_btn.clicked.connect(lambda _c, ax=a: self._move(ax))
            grid.addWidget(move_btn, a, 2)
            # The +/- buttons step BY the step amount (bottom row) and carry it
            # on their face, so nobody mistakes them for "move by the box".
            minus, plus = QPushButton("−"), QPushButton("+")
            for b in (minus, plus):
                b.setMinimumWidth(84)
                b.setToolTip("a relative move BY the step amount below")
            minus.clicked.connect(lambda _c, ax=a: self._step(ax, -1))
            plus.clicked.connect(lambda _c, ax=a: self._step(ax, +1))
            self._jog_buttons.append((minus, plus))
            grid.addWidget(minus, a, 3)
            grid.addWidget(plus, a, 4)
            zero = QPushButton("Zero")
            zero.setToolTip("display origin here (read-out only)")
            zero.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.set_zero(ax)))
            grid.addWidget(zero, a, 5)
            datum = QPushButton("Datum")
            datum.setToolTip("reset the controller's step counter to 0 here (ZP)")
            datum.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.zero_counter(ax)))
            grid.addWidget(datum, a, 6)
        lay.addLayout(grid)
        self._unit.currentTextChanged.connect(self._on_unit_changed)

        row = QHBoxLayout()
        self._jog_lbl = QLabel("step amount (steps)")
        row.addWidget(self._jog_lbl)
        self._jog_step = _spin(self.cfg.motion.jog_steps, 0, 10_000_000, 10, 0)
        self._jog_step.valueChanged.connect(lambda _v: self._label_step_buttons())
        row.addWidget(self._jog_step)
        self._jog_shown_um = False
        row.addStretch(1)
        zero_all = QPushButton("Zero all")
        zero_all.clicked.connect(lambda: self._do(self.ctrl.set_zero_all))
        row.addWidget(zero_all)
        datum_all = QPushButton("Datum all")
        datum_all.clicked.connect(lambda: self._do(self.ctrl.zero_counter_all))
        row.addWidget(datum_all)
        stop = QPushButton("STOP")
        stop.setObjectName("danger")
        repolish(stop)
        stop.clicked.connect(lambda: self._do(self.ctrl.stop_all))
        row.addWidget(stop)
        lay.addLayout(row)
        self._cal_hint = QLabel("")
        self._cal_hint.setObjectName("hint")
        self._cal_hint.setWordWrap(True)
        lay.addWidget(self._cal_hint)
        self._on_unit_changed(self._unit.currentText())
        return frame

    def _build_jog_card(self) -> QFrame:
        frame, lay = _card("JOG  (hold the button · releases itself if the link drops)")
        row = QHBoxLayout()
        row.addWidget(QLabel("speed"))
        self._jog_speed = QComboBox()
        for mode, text in JOG_SPEEDS.items():
            self._jog_speed.addItem(text, mode)
        idx = self._jog_speed.findData(int(self.cfg.motion.jog_speed))
        self._jog_speed.setCurrentIndex(max(idx, 0))
        row.addWidget(self._jog_speed, 1)
        self._hold_buttons = []
        for a in range(2):
            for sign, text in ((-1, f"◀ {AXES[a]}−"), (+1, f"{AXES[a]}+ ▶")):
                b = QPushButton(text)
                b.setMinimumHeight(34)
                b.pressed.connect(lambda ax=a, s=sign: self._jog_press(ax, s))
                b.released.connect(lambda ax=a: self._jog_release(ax))
                row.addWidget(b)
                self._hold_buttons.append(b)
        lay.addLayout(row)
        self._jog_speed.setToolTip("Speeds 2 and 3 use the MAXIMUM amplitude, not yours: "
                                   "the step count is right, the µm conversion is not.")
        return frame

    def _build_amplitude_card(self) -> QFrame:
        frame, lay = _card("STEP AMPLITUDE  (SU · 1..50 · sets the step size)")
        grid = QGridLayout()
        grid.addWidget(QLabel("forward"), 0, 1)
        grid.addWidget(QLabel("backward"), 0, 2)
        self._amp_boxes: list[tuple] = []
        for a in range(2):
            grid.addWidget(QLabel(AXES[a]), a + 1, 0)
            f = _amp_spin(getattr(self.cfg.motion, f"amp_fwd_{AXES[a].lower()}"))
            b = _amp_spin(getattr(self.cfg.motion, f"amp_bwd_{AXES[a].lower()}"))
            grid.addWidget(f, a + 1, 1)
            grid.addWidget(b, a + 1, 2)
            self._amp_boxes.append((f, b))
        apply = QPushButton("Apply amplitudes")
        apply.setObjectName("primary")
        repolish(apply)
        apply.clicked.connect(self._apply_amplitudes)
        grid.addWidget(apply, 1, 3)
        self._steps_btn = QPushButton("Steps: Small")
        self._steps_btn.setCheckable(True)
        self._steps_btn.setToolTip("preset: every amplitude to the large / small value "
                                   "(Settings > Motion)")
        self._steps_btn.toggled.connect(self._toggle_steps)
        grid.addWidget(self._steps_btn, 2, 3)
        grid.setColumnStretch(4, 1)
        lay.addLayout(grid)
        self._amp_hint = QLabel("")
        self._amp_hint.setObjectName("hint")
        self._amp_hint.setWordWrap(True)
        lay.addWidget(self._amp_hint)
        self._suppress_presets = False
        self._preset_state = None
        return frame

    def _build_step_size_card(self) -> QFrame:
        """Type in a MEASURED step size, per direction.

        Stored together with the amplitude in force, because a step size is
        only true at the amplitude it was measured at.
        """
        frame, lay = _card("STEP SIZE  (µm per step · measured · per direction)")
        grid = QGridLayout()
        grid.addWidget(QLabel("forward"), 0, 1)
        grid.addWidget(QLabel("backward  (0 = same)"), 0, 2)
        grid.addWidget(QLabel("measured at amp"), 0, 3)
        self._cal_boxes: list[tuple] = []
        self._cal_amp_lbl = []
        c = self.cfg.calibration
        for a in range(2):
            low = AXES[a].lower()
            grid.addWidget(QLabel(AXES[a]), a + 1, 0)
            fwd = _spin(getattr(c, f"um_per_step_{low}"), 1e-6, 1000.0, 0.001, 5)
            bwd = _spin(getattr(c, f"um_per_step_{low}_bwd"), 0.0, 1000.0, 0.001, 5)
            bwd.setSpecialValueText("same")
            grid.addWidget(fwd, a + 1, 1)
            grid.addWidget(bwd, a + 1, 2)
            lbl = QLabel("")
            grid.addWidget(lbl, a + 1, 3)
            self._cal_boxes.append((fwd, bwd))
            self._cal_amp_lbl.append(lbl)
        apply = QPushButton("Store step sizes")
        apply.setObjectName("primary")
        repolish(apply)
        apply.clicked.connect(self._apply_step_sizes)
        grid.addWidget(apply, 1, 4)
        grid.setColumnStretch(5, 1)
        lay.addLayout(grid)
        hint = QLabel("Measure at the amplitude you will use: move N steps, measure the "
                      "distance (microscope feature, dial gauge), divide by N -- each "
                      "direction separately. Stored with the amplitude it applies to.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return frame

    def _build_limit_card(self) -> QFrame:
        """AG-LS25 limit switch: MV to a limit, MA / PA, step-size routine."""
        frame, lay = _card("LIMIT SWITCH  (AG-LS25 · MV · MA · PA · step size)")
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        self._pa_target = []
        self._limit_buttons = []
        for a in range(2):
            grid.addWidget(QLabel(AXES[a]), a, 0)
            lo, hi = QPushButton("◀ limit"), QPushButton("limit ▶")
            lo.setToolTip("MV-3: fast to the negative limit switch, stops there")
            hi.setToolTip("MV3: fast to the positive limit switch, stops there")
            lo.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.move_to_limit(ax, -1, 3)))
            hi.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.move_to_limit(ax, +1, 3)))
            meas = QPushButton("Measure step size")
            meas.setToolTip("limit to limit and back at the amplitudes in force: stores "
                            "forward AND backward step size; ends at the − limit with "
                            "the datum there (minutes)")
            meas.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.measure_step_size(ax)))
            ma = QPushButton("MA")
            ma.setToolTip("the controller measures the absolute position (limit to "
                          "limit; the USB link is cut for up to 2 min)")
            ma.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.measure_position(ax)))
            pa = _spin(0.0, 0.0, float(self.cfg.hardware.travel_um), 100.0, 0)
            pa.setToolTip("absolute target, µm from the − limit (resolution travel/1000)")
            go = QPushButton("PA")
            go.setToolTip("absolute move to the target on the left (accuracy ~100 µm)")
            go.clicked.connect(lambda _c, ax=a, sp=pa: self._do(
                lambda: self.ctrl.move_absolute(ax, sp.value())))
            for col, w in enumerate((lo, hi, meas, ma, pa, go), start=1):
                grid.addWidget(w, a, col)
            self._pa_target.append(pa)
            self._limit_buttons += [lo, hi, meas, ma, go]
        grid.setColumnStretch(7, 1)
        lay.addLayout(grid)
        self._limit_hint = QLabel("")
        self._limit_hint.setObjectName("hint")
        self._limit_hint.setWordWrap(True)
        lay.addWidget(self._limit_hint)
        if not self.cfg.hardware.has_limit_switch:
            for b in self._limit_buttons:
                b.setEnabled(False)
        return frame

    def _build_leash_card(self) -> QFrame:
        frame, lay = _card("LEASH  (limit travel to a box around the Datum)")
        row = QHBoxLayout()
        self._leash_on = QCheckBox("armed")
        self._leash_on.setToolTip("When armed each axis may only move ± this many steps "
                                  "from the Datum. Set the Datum at a safe spot first.")
        self._leash_on.setChecked(self.cfg.limits.leash_enabled)
        row.addWidget(self._leash_on)
        row.addWidget(QLabel("± steps"))
        self._leash_steps = _spin(self.cfg.limits.leash_steps, 0, 10_000_000, 1000, 0)
        row.addWidget(self._leash_steps)
        row.addStretch(1)
        apply = QPushButton("Apply")
        apply.setObjectName("primary")
        repolish(apply)
        apply.clicked.connect(self._apply_leash)
        row.addWidget(apply)
        lay.addLayout(row)
        self._leash_hint = QLabel("")
        self._leash_hint.setObjectName("muted")
        self._leash_hint.setWordWrap(True)
        lay.addWidget(self._leash_hint)
        return frame

    def _build_positions_card(self) -> QFrame:
        frame, lay = _card("POSITION LIST  (20 slots, step coords)")
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["#", "name", "X", "Y"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setMinimumHeight(110)
        lay.addWidget(self._table, 1)
        row = QHBoxLayout()
        self._slot_name = QLineEdit()
        self._slot_name.setPlaceholderText("optional name for selected slot")
        store = QPushButton("Store")
        store.setObjectName("primary")
        repolish(store)
        goto, clear = QPushButton("Go to"), QPushButton("Clear")
        store.clicked.connect(self._store_current)
        goto.clicked.connect(self._goto_selected)
        clear.clicked.connect(self._clear_selected)
        row.addWidget(self._slot_name, 1)
        for b in (store, goto, clear):
            row.addWidget(b)
        lay.addLayout(row)
        frow = QHBoxLayout()
        save, load = QPushButton("Save list…"), QPushButton("Load list…")
        save.clicked.connect(self._save_positions)
        load.clicked.connect(self._load_positions)
        frow.addStretch(1)
        frow.addWidget(save)
        frow.addWidget(load)
        lay.addLayout(frow)
        return frame

    def _build_log_card(self) -> QFrame:
        frame, lay = _card("LOG")
        self._log = QPlainTextEdit()
        self._log.setObjectName("log")
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        self._log.setMinimumHeight(50)
        lay.addWidget(self._log)
        return frame

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #
    def _open_settings(self) -> None:
        if self.remote:
            # start from what the service uses NOW (it adopted the controller's
            # amplitudes), or OK would push this window's stale copy back
            from ..net.protocol import apply_config_dict
            self._do(lambda: apply_config_dict(self.cfg, self.ctrl.get_config()))
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            if self.remote:
                from ..net.protocol import config_to_dict
                self._do(lambda: self.ctrl.set_config(config_to_dict(self.cfg)))
            else:
                self._do(self.ctrl.apply_config)
            self._on_event("info", "settings applied")

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def _do(self, fn):
        try:
            return fn()
        except Exception as exc:
            self._on_event("error", f"{type(exc).__name__}: {exc}")
            return None

    def _axis_cal(self, axis: int, direction: int = 0) -> float:
        """um per step as the SERVICE uses it (status), per direction."""
        st = self._live
        if st is not None:
            table = (st.um_per_step_fwd if direction > 0 else
                     st.um_per_step_bwd if direction < 0 else st.um_per_step)
            if table[axis] > 0:
                return float(table[axis])
        return 0.05

    def _on_unit_changed(self, text: str) -> None:
        steps = text == "steps"
        for sp in self._target:
            sp.setDecimals(0 if steps else 3)
            sp.setSingleStep(1.0 if steps else 0.1)
        um = not steps
        cal = self._axis_cal(0)
        if um and not self._jog_shown_um:
            self._jog_step.setDecimals(3)
            self._jog_step.setValue(self._jog_step.value() * cal)
        elif not um and self._jog_shown_um:
            v = round(self._jog_step.value() / cal)
            self._jog_step.setDecimals(0)
            self._jog_step.setValue(v)
        self._jog_step.setSingleStep(0.5 if um else 10)
        self._jog_lbl.setText("step amount (µm)" if um else "step amount (steps)")
        self._jog_shown_um = um
        self._label_step_buttons()

    def _label_step_buttons(self) -> None:
        um = self._unit.currentText() == "µm"
        amount = f"{self._jog_step.value():g} µm" if um else f"{int(self._jog_step.value())} st"
        for minus, plus in self._jog_buttons:
            minus.setText(f"− {amount}")
            plus.setText(f"+ {amount}")

    def _move(self, axis: int) -> None:
        val = self._target[axis].value()
        um = self._unit.currentText() == "µm"
        rel = self._rel_mode.isChecked()
        if um and rel:
            self._do(lambda: self.ctrl.move_relative_um(axis, val))
        elif um:
            self._do(lambda: self.ctrl.move_to_um(axis, val))
        elif rel:
            self._do(lambda: self.ctrl.move_steps(axis, int(round(val))))
        else:
            self._do(lambda: self.ctrl.move_to_step(axis, int(round(val))))

    def _step(self, axis: int, sign: int) -> None:
        if self._unit.currentText() == "µm":
            self._do(lambda: self.ctrl.move_relative_um(axis, sign * self._jog_step.value()))
        else:
            self._do(lambda: self.ctrl.move_steps(axis, sign * int(round(self._jog_step.value()))))

    # -- hold-to-jog --------------------------------------------------------- #
    def _jog_mode(self, sign: int) -> int:
        return sign * int(self._jog_speed.currentData() or 1)

    def _jog_press(self, axis: int, sign: int) -> None:
        mode = self._jog_mode(sign)
        self._held[axis] = mode
        self._do(lambda: self.ctrl.jog(axis, mode))
        if not self._keepalive.isActive():
            self._keepalive.start()

    def _jog_release(self, axis: int) -> None:
        self._held.pop(axis, None)
        self._do(lambda: self.ctrl.jog(axis, 0))
        if not self._held:
            self._keepalive.stop()

    def _keep_jogging(self) -> None:
        for axis, mode in list(self._held.items()):
            self._do(lambda a=axis, m=mode: self.ctrl.jog(a, m))

    # -- amplitude + step size ------------------------------------------------- #
    def _apply_amplitudes(self) -> None:
        for a, (f, b) in enumerate(self._amp_boxes):
            self._do(lambda a=a, v=f.value(): self.ctrl.set_amplitude(a, v, +1))
            self._do(lambda a=a, v=b.value(): self.ctrl.set_amplitude(a, v, -1))

    def _toggle_steps(self, checked: bool) -> None:
        if self._suppress_presets:
            return
        self._do(lambda: self.ctrl.set_step_size(bool(checked)))
        self._paint_steps_button(checked)

    def _paint_steps_button(self, large: bool) -> None:
        self._steps_btn.setText("Steps: Large" if large else "Steps: Small")
        self._steps_btn.setObjectName("primary" if large else "")
        repolish(self._steps_btn)

    def _apply_step_sizes(self) -> None:
        for a, (fwd, bwd) in enumerate(self._cal_boxes):
            b = bwd.value()
            if b > 0:
                self._do(lambda a=a, v=fwd.value(): self.ctrl.set_calibration(a, v, +1))
                self._do(lambda a=a, v=b: self.ctrl.set_calibration(a, v, -1))
            else:
                self._do(lambda a=a, v=fwd.value(): self.ctrl.set_calibration(a, v, 0))
        if self.remote:
            # a remote GUI edited the SERVICE's config: mirror it back locally
            from ..net.protocol import apply_config_dict
            cfg = self._do(self.ctrl.get_config)
            if cfg:
                apply_config_dict(self.cfg, cfg)

    def _apply_leash(self) -> None:
        enabled = self._leash_on.isChecked()
        steps = int(round(self._leash_steps.value()))
        self._do(lambda: self.ctrl.set_leash(enabled=enabled, leash_steps=steps))
        self.cfg.limits.leash_enabled = enabled
        self.cfg.limits.leash_steps = steps

    # -- position list ------------------------------------------------------- #
    def _selected_slot(self) -> int:
        row = self._table.currentRow()
        return row if row >= 0 else 0

    def _store_current(self) -> None:
        slot = self._selected_slot()
        name = self._slot_name.text().strip()
        self._do(lambda: self.ctrl.store_position(slot, name))
        self._reload_positions()

    def _goto_selected(self) -> None:
        self._do(lambda: self.ctrl.goto_position(self._selected_slot()))

    def _clear_selected(self) -> None:
        self._do(lambda: self.ctrl.clear_position(self._selected_slot()))
        self._reload_positions()

    def _save_positions(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save position list", "positions.json",
                                              "JSON (*.json)")
        if path:
            self._do(lambda: self.ctrl.save_positions(path))

    def _load_positions(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load position list", "", "JSON (*.json)")
        if path:
            self._do(lambda: self.ctrl.load_positions(path))
            self._reload_positions()

    def _reload_positions(self) -> None:
        positions = self._do(self.ctrl.get_positions) or []
        self._table.setRowCount(len(positions))
        for i, p in enumerate(positions):
            used = p.get("used", False)
            cells = [str(i), p.get("name", "") if used else "—",
                     f"{int(p.get('x', 0))}" if used else "",
                     f"{int(p.get('y', 0))}" if used else ""]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if not used:
                    item.setForeground(QColor(theme.COLORS["muted"]))
                self._table.setItem(i, c, item)

    # ------------------------------------------------------------------ #
    # polling + events
    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        st = self.ctrl.status()
        self._live = st
        for a in range(2):
            self._big[a].setText(f"{st.position_um[a]:.3f}")
            state = "moving" if st.moving[a] else "idle"
            self._sub_lbl[a].setText(f"{int(st.position_steps[a])} steps · rel "
                                     f"{st.rel_um[a]:+.3f} µm · {state}")
        self._indicator.set_state(st.position_steps, st.moving, st.direction,
                                  st.amplitude_fwd, st.amplitude_bwd, st.limit_lo,
                                  st.limit_hi, st.leash, st.limit_switch, st.cal_valid)
        if st.hw_error:
            self._conn.setText(f"hardware error: {st.hw_error}")
            self._conn.setStyleSheet(f"color: {theme.COLORS['danger']}")
        else:
            self._conn.setText("connected" if st.connected else "not connected")
            self._conn.setStyleSheet(
                f"color: {theme.COLORS['ok' if st.connected else 'muted']}")
        parts = []
        for a in range(2):
            f, b = st.um_per_step_fwd[a] * 1000, st.um_per_step_bwd[a] * 1000
            parts.append(f"{AXES[a]} {f:.1f}/{b:.1f} nm" if abs(f - b) > 1e-6
                         else f"{AXES[a]} {f:.1f} nm")
        stale = [AXES[a] for a in range(2) if not st.cal_valid[a]]
        # steps already made at a non-calibrated amplitude (a changed amplitude,
        # or a jog at speed 2/3) stay in the estimate until the next datum
        drift = [AXES[a] for a in range(2) if st.cal_valid[a] and not st.estimate_ok[a]]
        self._cal_hint.setText(
            "µm ↔ steps: " + " · ".join(parts) + " per step (forward/backward)"
            + (f" — {', '.join(stale)} measured at another amplitude: µm are approximate"
               if stale else "")
            + (f" — {', '.join(drift)} moved at another amplitude since the datum: "
               "set the datum again to trust the µm" if drift else ""))
        for a in range(2):
            self._cal_amp_lbl[a].setText(f"+{st.cal_amp_fwd[a]} / -{st.cal_amp_bwd[a]}")
            self._cal_amp_lbl[a].setStyleSheet(
                f"color: {theme.COLORS['ok' if st.cal_valid[a] else 'accent_hi']}")
        self._amp_hint.setText(
            "now: " + " · ".join(f"{AXES[a]} +{st.amplitude_fwd[a]} / -{st.amplitude_bwd[a]}"
                                 for a in range(2))
            + ". Low amplitudes may not move at all; forward and backward differ.")
        for a, (f, b) in enumerate(self._amp_boxes):
            for box, v in ((f, st.amplitude_fwd[a]), (b, st.amplitude_bwd[a])):
                if not box.hasFocus() and box.value() != int(v):
                    box.blockSignals(True)
                    box.setValue(int(v))
                    box.blockSignals(False)
        if st.step_large != self._preset_state:
            self._preset_state = st.step_large
            self._suppress_presets = True
            self._steps_btn.setChecked(bool(st.step_large))
            self._suppress_presets = False
            self._paint_steps_button(bool(st.step_large))
        # limit switch: the running routine, else the last result + positions
        meas = " · ".join(
            f"{AXES[a]} {st.measured_um[a]:.0f} µm" for a in range(2)
            if st.measured_um and st.measured_um[a] is not None)
        if st.routine_running:
            text = f"RUNNING #{st.routine_id} {st.routine}: {st.routine_msg}"
            if st.usb_busy:
                text += "  (controller has cut the USB link, STOP unavailable until it answers)"
        else:
            text = (f"last: {st.routine} → {st.routine_error}" if st.routine else
                    "no routine run yet")
        sw = [AXES[a] for a in range(2) if st.limit_switch[a]]
        self._limit_hint.setText(
            text + (f" — measured from the − limit: {meas}" if meas else "")
            + (f" — switch closed: {', '.join(sw)}" if sw else "")
            + f" — travel {st.travel_um:g} µm")
        k = st.um_per_step[0]
        self._leash_hint.setText(
            f"ARMED — ±{st.leash_steps} steps (≈ ±{st.leash_steps * k:.1f} µm in X) from the datum."
            if st.leash else
            "off — travel limits apply. Tip: Datum at a safe spot, set the range, then arm.")

    def _on_event(self, level: str, msg: str) -> None:
        color = {"info": theme.COLORS["muted"], "warn": theme.COLORS["accent_hi"],
                 "error": theme.COLORS["danger"]}.get(level, theme.COLORS["text"])
        self._log.appendHtml(f'<span style="color:{color}">[{level}]</span> {msg}')


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def run_app(ctrl, cfg: Config, remote: bool = False) -> int:
    from PySide6.QtCore import QLocale
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from .theme import apply_window_icon
    apply_window_icon(app)
    # '.' decimal point, no thousands separator, whatever the Windows locale
    # (docs/DEVELOPER_NOTES.md gotcha #18).
    loc = QLocale.c()
    loc.setNumberOptions(QLocale.OmitGroupSeparator)
    QLocale.setDefault(loc)
    # Select the palette BEFORE any widget is built (set_theme mutates COLORS
    # in place, so widgets and painters pick up the active values).
    theme.set_theme(getattr(cfg.ui, "theme", "dark"))
    app.setStyle("Fusion")
    theme.apply_palette(app)
    app.setStyleSheet(theme.build_stylesheet())

    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """Local simulator front panel (what tools/render_all.py calls)."""
    from ..sim_system import build_sim_system

    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    brain, _ = build_sim_system(cfg)
    brain.start()
    try:
        return run_app(brain, cfg, remote=False)
    finally:
        brain.shutdown()
