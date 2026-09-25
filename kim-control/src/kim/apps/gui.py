"""The front panel (§7 of the guide): MainWindow + the signature indicator.

PySide6, Fusion style, dark/amber theme.  ``run_app(ctrl, cfg, remote=False)``
builds the window; ``ctrl`` is either a local :class:`Kim` brain or a
:class:`KimClient` -- they share a method surface, so the GUI is agnostic.

The panel is built around this module's idea of TWO LANGUAGES:
  * a MOVE card that lets you command absolute/relative targets in either STEPS
    or MICROMETRES (a unit selector + a "relative (from here)" toggle),
  * a DRIVE card exposing the native KIM101 knobs (step rate, acceleration,
    drive voltage) AND the micrometre bridge (um/step calibration, velocity in
    um/s).

Threading rule: instrument events arrive on a background thread, so they MUST
cross into Qt through a signal -- see :class:`Bridge`.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
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
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from . import theme
from .settings_dialog import SettingsDialog
from .theme import repolish

AXES = ("X", "Y", "Z")


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


# --------------------------------------------------------------------------- #
# signature indicator widget
# --------------------------------------------------------------------------- #
class InertiaIndicator(QWidget):
    """Top-down XY step-space map + a Z bar, with a discrete "stepping" pulse.

    The envelope is the travel limit (in steps); the marker is the live step
    position.  While an axis moves the marker emits expanding SQUARE rings --
    a deliberately "ratchety" motif that reads as discrete stepping (contrast
    the smooth round halo of the closed-loop stages).  Its own ~33 ms QTimer
    drives the animation, independent of the status poll.  Corner dots glow
    amber for axes that are moving.
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setMinimumHeight(210)
        self._pos = [0, 0, 0]          # step position
        self._moving = [False, False, False]
        self._lo = [0, 0, 0]           # effective lower bound per axis (steps)
        self._hi = [1, 1, 1]           # effective upper bound per axis (steps)
        self._leash = False
        self._phase = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, pos, moving, lo=None, hi=None, leash=False) -> None:
        self._pos = list(pos)
        self._moving = list(moving)
        if lo is not None:
            self._lo = list(lo)
        if hi is not None:
            self._hi = list(hi)
        self._leash = bool(leash)
        if any(moving):
            if not self._timer.isActive():
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()
            self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.10) % 1.0
        self.update()

    def _half(self, axis: int) -> float:
        """Symmetric half-span used for display: the axis is drawn as
        [-half, +half] so the datum (step 0) always sits at the CENTRE."""
        return max(abs(self._lo[axis]), abs(self._hi[axis]), 1.0)

    def _frac(self, axis: int) -> float:
        """Map a step position onto 0..1 with 0 pinned to the centre (0.5)."""
        half = self._half(axis)
        val = self._pos[axis]
        return min(1.0, max(0.0, 0.5 + val / (2.0 * half)))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        margin = 16
        z_bar_w = 26
        plot = _Rect(margin, margin, w - margin * 3 - z_bar_w, h - margin * 2)

        # amber border while the leash is arming the travel box
        border_col = QColor(theme.COLORS["accent"]) if self._leash else QColor(theme.COLORS["border"])
        p.setPen(QPen(border_col, 1))
        p.setBrush(QColor(theme.COLORS["code_bg"]))
        p.drawRoundedRect(plot.x, plot.y, plot.w, plot.h, 8, 8)

        p.setPen(QPen(QColor(theme.COLORS["grid"]), 1))
        for i in range(1, 4):
            gx = plot.x + plot.w * i / 4
            gy = plot.y + plot.h * i / 4
            p.drawLine(int(gx), plot.y, int(gx), plot.y + plot.h)
            p.drawLine(plot.x, int(gy), plot.x + plot.w, int(gy))

        # datum crosshair at the CENTRE (step 0) -- the graph is symmetric about it
        ccx = plot.x + plot.w / 2.0
        ccy = plot.y + plot.h / 2.0
        p.setPen(QPen(QColor(theme.COLORS["accent_dim"]), 1, Qt.DashLine))
        p.drawLine(int(ccx), plot.y, int(ccx), plot.y + plot.h)
        p.drawLine(plot.x, int(ccy), plot.x + plot.w, int(ccy))

        # a small tag so the leash state is unmistakable
        tag = f"LEASH ±{int(self._half(0))}" if self._leash else "full travel"
        p.setPen(QColor(theme.COLORS["accent_hi"]) if self._leash else QColor(theme.COLORS["muted"]))
        p.drawText(plot.x + plot.w - 118, plot.y + 14, f"{tag}")

        fx = self._frac(0)
        fy = self._frac(1)
        mx = plot.x + fx * plot.w
        my = plot.y + (1.0 - fy) * plot.h

        moving = any(self._moving)
        base = QColor(theme.COLORS["accent"]) if moving else QColor(theme.COLORS["accent_dim"])

        p.setPen(QPen(QColor(theme.COLORS["muted"]), 1, Qt.DashLine))
        p.drawLine(plot.x, int(my), plot.x + plot.w, int(my))
        p.drawLine(int(mx), plot.y, int(mx), plot.y + plot.h)

        # discrete "step" rings: two expanding squares out of phase
        if moving:
            for k in (0, 1):
                t = (self._phase + 0.5 * k) % 1.0
                side = 8 + 34 * t
                col = QColor(theme.COLORS["accent"])
                col.setAlpha(int(150 * (1.0 - t)))
                p.setPen(QPen(col, 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(int(mx - side / 2), int(my - side / 2), int(side), int(side))

        # the marker itself is a small square (a "step"), not a round dot
        p.setPen(QPen(QColor(theme.COLORS["accent_hi"]), 2))
        p.setBrush(base)
        p.drawRect(int(mx - 6), int(my - 6), 12, 12)

        # Z bar -- also symmetric: the datum (0) is the CENTRE line, fill grows
        # up for positive Z and down for negative Z.
        zx = plot.x + plot.w + margin
        p.setPen(QPen(border_col, 1))
        p.setBrush(QColor(theme.COLORS["code_bg"]))
        p.drawRoundedRect(zx, plot.y, z_bar_w, plot.h, 6, 6)
        zc = plot.y + plot.h / 2.0
        p.setPen(QPen(QColor(theme.COLORS["accent_dim"]), 1, Qt.DashLine))
        p.drawLine(zx, int(zc), zx + z_bar_w, int(zc))
        fz = self._frac(2)
        zlevel = plot.y + (1.0 - fz) * plot.h
        top = min(zc, zlevel)
        bot = max(zc, zlevel)
        if bot - top < 1:
            bot = top + 1
        zcol = QColor(theme.COLORS["accent"]) if self._moving[2] else QColor(theme.COLORS["ok"])
        p.setPen(Qt.NoPen)
        p.setBrush(zcol)
        p.drawRoundedRect(zx + 3, int(top), z_bar_w - 6, int(bot - top), 3, 3)
        p.setPen(QColor(theme.COLORS["muted"]))
        p.drawText(zx - 2, plot.y + plot.h + 13, "Z")

        p.setPen(QColor(theme.COLORS["muted"]))
        p.drawText(plot.x, plot.y + plot.h + 13, "X →")
        p.save()
        p.translate(plot.x - 4, plot.y + plot.h)
        p.rotate(-90)
        p.drawText(0, 0, "Y →")
        p.restore()

        # per-axis moving dots
        for a in range(3):
            dot = QColor(theme.COLORS["accent"]) if self._moving[a] else QColor(theme.COLORS["accent_dim"])
            p.setPen(Qt.NoPen)
            p.setBrush(dot)
            p.drawEllipse(plot.x + 6 + a * 16, plot.y + 6, 8, 8)
        p.end()


class _Rect:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = int(x), int(y), int(w), int(h)


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class MainWindow(QWidget):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self.remote = remote
        self.setWindowTitle("3D Inertia Stage (KIM101 / PIA25)" + ("  [remote]" if remote else ""))
        self.resize(830, 900)

        self._bridge = Bridge()
        self._bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda level, msg: self._bridge.event.emit(level, msg)

        self._build_ui()

        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()

        self._reload_positions()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("3D INERTIA STAGE  ·  KIM101 / PIA25")
        title.setObjectName("cardTitle")
        top.addWidget(title)
        top.addStretch(1)
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        top.addWidget(settings_btn)
        root.addLayout(top)

        controls = QWidget()
        col = QVBoxLayout(controls)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        self._suppress_presets = False
        self._preset_state = None
        col.addWidget(self._build_readout_card())
        col.addWidget(self._build_move_card())
        col.addWidget(self._build_presets_card())
        col.addWidget(self._build_leash_card())
        col.addWidget(self._build_step_size_card())
        col.addWidget(self._build_calibration_card())
        col.addWidget(self._build_positions_card(), 1)
        col.addWidget(self._build_log_card(), 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(controls)
        root.addWidget(scroll, 1)

        # match the leash boxes to the initial unit (default µm)
        self._sync_leash_units()

    def _build_readout_card(self) -> QFrame:
        frame, lay = _card("POSITION  (µm · steps · rel)")
        self._big = []
        self._sub_lbl = []
        row = QHBoxLayout()
        for a in range(3):
            c = QVBoxLayout()
            cap = QLabel(AXES[a])
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

        self._indicator = InertiaIndicator(self.cfg)
        lay.addWidget(self._indicator)
        return frame

    def _build_move_card(self) -> QFrame:
        frame, lay = _card("MOVE  /  JOG  /  ZERO")

        # global mode row: unit + relative toggle
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
        for a in range(3):
            grid.addWidget(QLabel(AXES[a]), a, 0)
            sp = _spin(0.0, -1e9, 1e9, 1.0, 3)
            grid.addWidget(sp, a, 1)
            self._target.append(sp)

            move_btn = QPushButton("Move")
            move_btn.setMaximumWidth(58)
            move_btn.clicked.connect(lambda _c, ax=a: self._move(ax))
            grid.addWidget(move_btn, a, 2)

            # The ± buttons jog by the JOG STEP (bottom row), not by the value in
            # this row's box -- so they carry that amount on their face. With a
            # bare "−"/"+" Lukáš typed 0.2 um, pressed +, and got the 2 um jog
            # step (2026-09-14).
            minus = QPushButton("−")
            plus = QPushButton("+")
            minus.setMinimumWidth(74)
            plus.setMinimumWidth(74)
            minus.setToolTip("jog BY the jog step (bottom row); the box on the left is for Move")
            plus.setToolTip("jog BY the jog step (bottom row); the box on the left is for Move")
            self._jog_buttons.append((minus, plus))
            minus.clicked.connect(lambda _c, ax=a: self._jog(ax, -1))
            plus.clicked.connect(lambda _c, ax=a: self._jog(ax, +1))
            grid.addWidget(minus, a, 3)
            grid.addWidget(plus, a, 4)

            zero = QPushButton("Zero")
            zero.setMaximumWidth(60)
            zero.setToolTip("set this axis' display origin here (read-out only)")
            zero.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.set_zero(ax)))
            grid.addWidget(zero, a, 5)

            datum = QPushButton("Datum")
            datum.setMaximumWidth(78)
            datum.setToolTip("reset the hardware step counter to 0 here")
            datum.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.zero_counter(ax)))
            grid.addWidget(datum, a, 6)
        lay.addLayout(grid)

        # keep the target boxes' precision honest: steps are integers (0 dp),
        # micrometres keep 3 dp.  Re-applied whenever the unit combo changes.
        self._unit.currentTextChanged.connect(self._on_unit_changed)
        self._on_unit_changed(self._unit.currentText())

        row = QHBoxLayout()
        self._jog_lbl = QLabel("jog step (steps)")
        row.addWidget(self._jog_lbl)
        self._jog_step = _spin(self.cfg.motion.jog_steps, 0, 10_000_000, 10, 0)
        row.addWidget(self._jog_step)
        # the jog field follows the same unit combo (steps <-> µm)
        self._jog_shown_um = False   # spin was just created holding a step count
        self._jog_step.valueChanged.connect(lambda _v: self._label_jog_buttons())
        self._sync_jog_units()
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
        # Which step size the µm boxes use. It is NOT one number: the camera
        # calibration measures each direction, and they differ on a slip-stick
        # actuator -- so say so rather than let a µm jog look exact.
        self._cal_hint = QLabel("")
        self._cal_hint.setObjectName("hint")
        self._cal_hint.setWordWrap(True)
        lay.addWidget(self._cal_hint)
        return frame

    def _build_leash_card(self) -> QFrame:
        frame, lay = _card("LEASH  (limit travel to a box around the Datum)")
        row = QHBoxLayout()
        self._leash_on = QCheckBox("armed")
        self._leash_on.setToolTip(
            "When armed, each axis may only move ± the range below, measured from "
            "the Datum. Press Datum at a safe spot first to set the centre."
        )
        row.addWidget(self._leash_on)
        row.addSpacing(12)
        self._leash_xy_lbl = QLabel("XY ± steps")
        row.addWidget(self._leash_xy_lbl)
        self._leash_xy = _spin(self.cfg.limits.leash_xy, 0, 10_000_000, 1000, 0)
        row.addWidget(self._leash_xy)
        self._leash_z_lbl = QLabel("Z ± steps")
        row.addWidget(self._leash_z_lbl)
        self._leash_z = _spin(self.cfg.limits.leash_z, 0, 10_000_000, 1000, 0)
        row.addWidget(self._leash_z)
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

        self._leash_on.setChecked(self.cfg.limits.leash_enabled)
        return frame

    def _build_step_size_card(self) -> QFrame:
        """Type the step size in by hand -- the no-camera path.

        Forward and backward are separate boxes because a slip-stick actuator
        does not step equally both ways; leave backward at 0 to say "same as
        forward" (which is all a datasheet tells you). While a camera
        calibration is in charge these boxes are disabled and say so, rather
        than quietly storing numbers that nothing uses.
        """
        frame, lay = _card("STEP SIZE  (µm per step · type it in when you have no camera)")
        grid = QGridLayout()
        grid.addWidget(QLabel("forward"), 0, 1)
        grid.addWidget(QLabel("backward  (0 = same)"), 0, 2)
        self._cal_boxes: list[tuple] = []
        for a, name in enumerate(("X", "Y", "Z")):
            grid.addWidget(QLabel(name), a + 1, 0)
            fwd = _spin(0.02, 1e-6, 1000.0, 0.001, 5)
            bwd = _spin(0.0, 0.0, 1000.0, 0.001, 5)
            bwd.setSpecialValueText("same")     # 0 reads as "same", not "0"
            grid.addWidget(fwd, a + 1, 1)
            grid.addWidget(bwd, a + 1, 2)
            self._cal_boxes.append((fwd, bwd))
        grid.setColumnStretch(3, 1)
        apply = QPushButton("Apply step sizes")
        apply.setObjectName("primary")
        apply.clicked.connect(self._apply_step_sizes)
        grid.addWidget(apply, 3, 3)
        lay.addLayout(grid)
        self._cal_manual_hint = QLabel("")
        self._cal_manual_hint.setObjectName("hint")
        self._cal_manual_hint.setWordWrap(True)
        lay.addWidget(self._cal_manual_hint)
        self._sync_step_size_boxes()
        return frame

    def _sync_step_size_boxes(self) -> None:
        """Show the numbers in force, and disable them while the camera rules."""
        c = self.cfg.calibration
        values = ((c.um_per_step_x, c.um_per_step_x_bwd),
                  (c.um_per_step_y, c.um_per_step_y_bwd),
                  (c.um_per_step_z, c.um_per_step_z_bwd))
        src = getattr(self, "_cal_src", ["config"] * 3)
        live = getattr(self, "_cal_live", None)
        if live:
            # An axis driven by the camera shows the CAMERA's numbers (greyed):
            # showing the ignored config value next to "disabled" would read as
            # if THAT were what the stage is doing.
            fwd_live, bwd_live, _mean = live
            values = tuple((fwd_live[a], bwd_live[a]) if src[a] == "camera" else values[a]
                           for a in range(3))
        for (fwd, bwd), (f, b) in zip(self._cal_boxes, values):
            for box, v in ((fwd, f), (bwd, b)):
                if not box.hasFocus():
                    box.blockSignals(True)
                    box.setValue(v)
                    box.blockSignals(False)
        camera = [n for n, s in zip("XYZ", getattr(self, "_cal_src", ["config"] * 3))
                  if s == "camera"]
        for a, (fwd, bwd) in enumerate(self._cal_boxes):
            live = "XYZ"[a] in camera
            fwd.setEnabled(not live)
            bwd.setEnabled(not live)
        if camera:
            self._cal_manual_hint.setText(
                f"{', '.join(camera)} follow the CAMERA calibration below, so these boxes are "
                f"disabled for them. Untick 'use_px_calibration' in Settings ▸ Calibration to "
                f"type your own instead.")
        else:
            self._cal_manual_hint.setText(
                "Measure how far the stage really goes (a known feature under the microscope, "
                "a dial gauge, an interferometer): move N steps, divide the distance by N, "
                "each direction separately. Saved with the config.")

    def _apply_step_sizes(self) -> None:
        for a, (fwd, bwd) in enumerate(self._cal_boxes):
            if not fwd.isEnabled():
                continue
            b = bwd.value()
            if b > 0:
                self._do(lambda a=a, v=fwd.value(): self.ctrl.set_calibration(a, v, +1))
                self._do(lambda a=a, v=b: self.ctrl.set_calibration(a, v, -1))
            else:   # "same": one number again for this axis
                self._do(lambda a=a, v=fwd.value(): self.ctrl.set_calibration(a, v, 0))
        # a remote GUI edits the SERVICE's config, so pull it back to stay in step
        self._do(self.ctrl.get_config)
        self._sync_step_size_boxes()

    def _build_calibration_card(self) -> QFrame:
        frame, lay = _card("CAMERA CALIBRATION  (step size in µm · X/Y · per voltage)")
        hint = QLabel(
            "Moves X and Y and watches the camera image: step size, direction and "
            "crosstalk for every voltage, forward and backward. Needs the camera "
            "service running on a focused, textured area with the stabiliser OFF. "
            "Takes ~2 min per voltage. STOP aborts."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        row = QHBoxLayout()
        row.addWidget(QLabel("camera"))
        self._cal_host = QLineEdit("127.0.0.1")
        self._cal_host.setMaximumWidth(120)
        row.addWidget(self._cal_host)
        self._cal_port = _spin(5563, 1, 65535, 1, 0)
        row.addWidget(self._cal_port)
        row.addSpacing(10)
        row.addWidget(QLabel("voltages (V)"))
        self._cal_volts = QLineEdit("85, 95, 105, 115, 125")
        row.addWidget(self._cal_volts, 1)
        row.addWidget(QLabel("repeats"))
        self._cal_reps = _spin(2, 1, 10, 1, 0)
        self._cal_reps.setMinimumWidth(40)
        row.addWidget(self._cal_reps)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self._cal_start = QPushButton("Start calibration")
        self._cal_start.setObjectName("primary")
        repolish(self._cal_start)
        self._cal_start.clicked.connect(self._start_calibration)
        row2.addWidget(self._cal_start)
        abort = QPushButton("Abort")
        abort.setObjectName("danger")
        repolish(abort)
        abort.clicked.connect(lambda: self._do(self.ctrl.abort_px_calibration))
        row2.addWidget(abort)
        self._cal_state = QLabel("")
        self._cal_state.setWordWrap(True)
        row2.addWidget(self._cal_state, 1)
        lay.addLayout(row2)

        # Micrometres, not pixels: px/step is what was measured, but nobody
        # thinks about the stage in camera pixels. The px value is in the tooltip.
        self._cal_table = QTableWidget(0, 8)
        self._cal_table.setHorizontalHeaderLabels(
            ["V", "X+ µm/step", "X- µm/step", "Y+ µm/step", "Y- µm/step",
             "X+ dir °", "Y+ dir °", "non-orth °"])
        self._cal_table.verticalHeader().setVisible(False)
        self._cal_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._cal_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._cal_table.setMinimumHeight(120)
        lay.addWidget(self._cal_table)
        self._cal_summary = QLabel("")
        self._cal_summary.setObjectName("muted")
        self._cal_summary.setWordWrap(True)
        lay.addWidget(self._cal_summary)

        self._cal_seen = None      # (calibrated, running) last shown
        return frame

    def _start_calibration(self) -> None:
        try:
            volts = [float(v) for v in self._cal_volts.text().replace(";", ",").split(",") if v.strip()]
        except ValueError:
            self._on_event("error", "voltages must be numbers separated by commas")
            return
        self._do(lambda: self.ctrl.start_px_calibration(
            camera_host=self._cal_host.text().strip() or "127.0.0.1",
            camera_port=int(self._cal_port.value()),
            voltages=volts, repeats=int(self._cal_reps.value())))

    def _reload_calibration(self) -> None:
        import numpy as np

        try:
            cal = self.ctrl.get_px_calibration()
        except Exception as exc:
            self._cal_summary.setText(f"(calibration unavailable: {exc})")
            return
        self._cal_table.setRowCount(0)
        if not cal:
            self._cal_summary.setText("not calibrated yet — image-frame moves "
                                      "(camera stabiliser on this rig) are refused until it is.")
            return
        from ..pxcal import axis_geometry

        px_um = float(cal.get("pixel_size_um") or 0.0)
        for key in sorted(cal["table"], key=float):
            cols = {d: np.array(c) for d, c in cal["table"][key].items()}
            g = axis_geometry(cols, px_um)
            r = self._cal_table.rowCount()
            self._cal_table.insertRow(r)
            steps = {}
            for d in ("X+", "X-", "Y+", "Y-"):
                px = float(np.hypot(*cols[d]))
                steps[d] = (f"{px * px_um:.4f}" if px_um else f"{px:.4f} px"), f"{px:.4f} px/step"
            vals = [(key, "")] + [steps[d] for d in ("X+", "X-", "Y+", "Y-")] + [
                (f"{g['X']['image_dir_deg']:+.1f}", "direction a + move sends the image"),
                (f"{g['Y']['image_dir_deg']:+.1f}", "direction a + move sends the image"),
                (f"{g['non_orthogonality_deg']:.2f}", "angle between X and Y, minus 90°")]
            for c, (text, tip) in enumerate(vals):
                item = QTableWidgetItem(text)
                if tip:
                    item.setToolTip(tip)
                self._cal_table.setItem(r, c, item)
        now = cal.get("now", {})
        g = now.get("geometry", {})
        worst = max((c["error_px"] for c in cal.get("validation", [])), default=float("nan"))
        ctx = cal.get("context", {})
        px_size = cal.get("pixel_size_um", 0) or 0
        in_use = now.get("um_per_step", {})
        self._cal_summary.setText(
            f"{cal.get('created', '')} · objective '{ctx.get('objective', '')}' · frame "
            f"{ctx.get('frame', '')} · µm = px × {px_size:.4g} µm/px (the camera's pixel "
            f"size when this was measured) · closed-loop check worst {worst:.1f} px"
            + (f" · at {now['voltage'][0]:g}/{now['voltage'][1]:g} V: X asym "
               f"{g['X']['asymmetry']:.2f}, Y asym {g['Y']['asymmetry']:.2f}" if g else "")
            + (f" · driving µm moves with X {in_use['X'][0] * 1000:.1f}/{in_use['X'][1] * 1000:.1f} nm, "
               f"Y {in_use['Y'][0] * 1000:.1f}/{in_use['Y'][1] * 1000:.1f} nm per step"
               if in_use else ""))

    def _build_presets_card(self) -> QFrame:
        frame, lay = _card("MOTION PRESETS  (all axes)")
        row = QHBoxLayout()
        self._speed_btn = QPushButton("Movement: Fast")
        self._speed_btn.setCheckable(True)
        self._speed_btn.setMinimumHeight(40)
        self._speed_btn.setToolTip("toggle the fast / slow movement preset "
                                   "(rates set in Settings)")
        self._speed_btn.toggled.connect(self._toggle_speed)
        row.addWidget(self._speed_btn, 1)
        self._steps_btn = QPushButton("Steps: Large")
        self._steps_btn.setCheckable(True)
        self._steps_btn.setMinimumHeight(40)
        self._steps_btn.setToolTip("toggle large / small steps "
                                   "(highest / lowest drive voltage)")
        self._steps_btn.toggled.connect(self._toggle_steps)
        row.addWidget(self._steps_btn, 1)
        lay.addLayout(row)

        self._preset_hint = QLabel("")
        self._preset_hint.setObjectName("muted")
        self._preset_hint.setWordWrap(True)
        lay.addWidget(self._preset_hint)

        # start the buttons in the mode implied by the loaded config
        m = self.cfg.motion
        fast0 = abs(m.rate_x - m.fast_rate) <= abs(m.rate_x - m.slow_rate)
        v = m.voltage_x
        large0 = abs(v - self.cfg.limits.max_voltage) <= abs(v - self.cfg.limits.min_voltage)
        self._sync_preset_buttons(fast0, large0)
        self._preset_state = (fast0, large0)
        return frame

    # -- preset toggles ---------------------------------------------------- #
    def _toggle_speed(self, checked: bool) -> None:
        if self._suppress_presets:
            return
        self._do(lambda: self.ctrl.set_speed(bool(checked)))
        self._paint_preset_buttons(checked, self._steps_btn.isChecked())

    def _toggle_steps(self, checked: bool) -> None:
        if self._suppress_presets:
            return
        self._do(lambda: self.ctrl.set_step_size(bool(checked)))
        self._paint_preset_buttons(self._speed_btn.isChecked(), checked)

    def _paint_preset_buttons(self, fast: bool, large: bool) -> None:
        self._speed_btn.setText("Movement: Fast" if fast else "Movement: Slow")
        self._steps_btn.setText("Steps: Large" if large else "Steps: Small")
        self._speed_btn.setObjectName("primary" if fast else "")
        self._steps_btn.setObjectName("primary" if large else "")
        repolish(self._speed_btn)
        repolish(self._steps_btn)
        m = self.cfg.motion
        rate = m.fast_rate if fast else m.slow_rate
        volt = self.cfg.limits.max_voltage if large else self.cfg.limits.min_voltage
        self._preset_hint.setText(
            f"movement ≈{rate:.0f} steps/s · step size at {volt:.0f} V. "
            "Per-axis rate / accel / voltage / calibration are in Settings."
        )

    def _sync_preset_buttons(self, fast: bool, large: bool) -> None:
        """Set the buttons to a state WITHOUT re-triggering the apply handlers."""
        self._suppress_presets = True
        self._speed_btn.setChecked(fast)
        self._steps_btn.setChecked(large)
        self._suppress_presets = False
        self._paint_preset_buttons(fast, large)

    def _build_positions_card(self) -> QFrame:
        frame, lay = _card("POSITION LIST  (20 slots, step coords)")
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["#", "name", "X", "Y", "Z"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setMinimumHeight(150)
        lay.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._slot_name = QLineEdit()
        self._slot_name.setPlaceholderText("optional name for selected slot")
        store = QPushButton("Store current")
        store.setObjectName("primary")
        repolish(store)
        goto = QPushButton("Go to")
        clear = QPushButton("Clear")
        store.clicked.connect(self._store_current)
        goto.clicked.connect(self._goto_selected)
        clear.clicked.connect(self._clear_selected)
        row.addWidget(QLabel("name"))
        row.addWidget(self._slot_name, 1)
        row.addWidget(store)
        row.addWidget(goto)
        row.addWidget(clear)
        lay.addLayout(row)

        frow = QHBoxLayout()
        save = QPushButton("Save list…")
        load = QPushButton("Load list…")
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
        self._log.setMinimumHeight(110)
        lay.addWidget(self._log)
        return frame

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self._push_config()
            self._sync_leash_fields()
            # re-apply the current presets so edited fast/slow + voltage values
            # (and any calibration change) take effect immediately
            self._do(lambda: self.ctrl.set_speed(self._speed_btn.isChecked()))
            self._do(lambda: self.ctrl.set_step_size(self._steps_btn.isChecked()))
            self._on_event("info", "settings applied")

    def _push_config(self) -> None:
        if self.remote:
            from ..net.protocol import config_to_dict
            self._do(lambda: self.ctrl.set_config(config_to_dict(self.cfg)))
        else:
            self._do(self.ctrl.apply_config)

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def _do(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self._on_event("error", f"{type(exc).__name__}: {exc}")

    def _on_unit_changed(self, text: str) -> None:
        """Steps are integers -> show 0 decimals; micrometres keep 3.

        The leash boxes follow the same unit (built after the move card, so
        guard until they exist)."""
        steps = text == "steps"
        for sp in self._target:
            val = sp.value()
            sp.setDecimals(0 if steps else 3)
            sp.setSingleStep(1.0 if steps else 0.1)
            if steps:
                sp.setValue(round(val))
        if hasattr(self, "_jog_step"):
            self._sync_jog_units()
        if hasattr(self, "_leash_xy"):
            self._sync_leash_units()

    def _move(self, axis: int) -> None:
        val = self._target[axis].value()
        um = self._unit.currentText() == "µm"
        rel = self._rel_mode.isChecked()
        if um and rel:
            self._do(lambda: self.ctrl.move_relative_um(axis, val))
        elif um and not rel:
            self._do(lambda: self.ctrl.move_to_um(axis, val))
        elif not um and rel:
            self._do(lambda: self.ctrl.move_steps(axis, int(round(val))))
        else:
            self._do(lambda: self.ctrl.move_to_step(axis, int(round(val))))

    def _axis_cal(self, axis: int, direction: int = 0) -> float:
        """µm per step as the SERVICE is using it (status), not as configured.

        The brain may be taking it from the camera calibration, per direction;
        a GUI that converted with the config value would send a step count that
        does not match the µm it shows.
        """
        live = getattr(self, "_cal_live", None)
        if live:
            fwd, bwd, mean = live
            table = fwd if direction > 0 else bwd if direction < 0 else mean
            if axis < len(table) and table[axis] > 0:
                return float(table[axis])
        c = self.cfg.calibration
        v = (c.um_per_step_x, c.um_per_step_y, c.um_per_step_z)[axis]
        return v if v > 0 else 0.02

    def _sync_jog_units(self) -> None:
        """Relabel + convert the jog-step box for the current unit, preserving
        the physical jog distance (uses X's calibration as the representative)."""
        um = self._unit.currentText() == "µm"
        cal = self._axis_cal(0)
        if um and not self._jog_shown_um:          # steps -> µm
            self._jog_step.setDecimals(3)
            self._jog_step.setSingleStep(0.5)
            self._jog_step.setValue(self._jog_step.value() * cal)
            self._jog_lbl.setText("jog step (µm)")
        elif (not um) and self._jog_shown_um:      # µm -> steps
            steps = round(self._jog_step.value() / cal)
            self._jog_step.setDecimals(0)
            self._jog_step.setSingleStep(10)
            self._jog_step.setValue(steps)
            self._jog_lbl.setText("jog step (steps)")
        else:                                      # same unit: just fix labels
            self._jog_step.setDecimals(3 if um else 0)
            self._jog_step.setSingleStep(0.5 if um else 10)
            self._jog_lbl.setText("jog step (µm)" if um else "jog step (steps)")
        self._jog_shown_um = um
        self._label_jog_buttons()

    def _update_cal_hint(self, st) -> None:
        """One line saying where the µm↔steps numbers come from, per axis."""
        src = list(getattr(st, "um_per_step_src", ["config"] * 3))
        fwd = list(getattr(st, "um_per_step_fwd", st.um_per_step))
        bwd = list(getattr(st, "um_per_step_bwd", st.um_per_step))
        parts = []
        for a, name in enumerate(("X", "Y", "Z")):
            if abs(fwd[a] - bwd[a]) > 1e-4 * max(fwd[a], bwd[a]):
                parts.append(f"{name} {fwd[a] * 1000:.1f}/{bwd[a] * 1000:.1f} nm")
            else:
                parts.append(f"{name} {fwd[a] * 1000:.1f} nm")
        camera = [n for n, s in zip("XYZ", src) if s == "camera"]
        where = (f"camera calibration for {', '.join(camera)}" if camera
                 else "configured values (datasheet)")
        self._cal_hint.setText(
            f"µm ↔ steps: {where} — " + " · ".join(parts) + " per step (forward/backward)")

    def _label_jog_buttons(self) -> None:
        um = self._unit.currentText() == "µm"
        amount = f"{self._jog_step.value():g} µm" if um else f"{int(self._jog_step.value())} st"
        for minus, plus in self._jog_buttons:
            minus.setText(f"− {amount}")
            plus.setText(f"+ {amount}")

    def _jog(self, axis: int, sign: int) -> None:
        if self._unit.currentText() == "µm":
            # the direction decides the step size: +2 µm and -2 µm are NOT the
            # same number of steps on this stage
            delta = int(round(self._jog_step.value() / self._axis_cal(axis, sign)))
        else:
            delta = int(round(self._jog_step.value()))
        self._do(lambda: self.ctrl.move_steps(axis, sign * delta))

    # -- the leash boxes follow the MOVE card's unit (steps or µm) ---------- #
    def _leash_is_um(self) -> bool:
        return self._unit.currentText() == "µm"

    def _leash_cal(self) -> tuple[float, float]:
        """(xy, z) µm-per-step used to convert the leash boxes.

        The leash range is stored as a single step count shared by X and Y, so
        the µm view of the XY box uses X's calibration as the representative one
        (X and Y are normally calibrated alike); Z uses its own.
        """
        c = self.cfg.calibration
        xy = c.um_per_step_x if c.um_per_step_x > 0 else 0.02
        z = c.um_per_step_z if c.um_per_step_z > 0 else 0.02
        return xy, z

    def _sync_leash_units(self) -> None:
        """Relabel + rescale the leash boxes for the current unit, showing the
        stored (authoritative, in steps) range converted to that unit."""
        um = self._leash_is_um()
        xy_cal, z_cal = self._leash_cal()
        xy_steps = self.cfg.limits.leash_xy
        z_steps = self.cfg.limits.leash_z
        if um:
            self._leash_xy_lbl.setText("XY ± µm")
            self._leash_z_lbl.setText("Z ± µm")
            for sp in (self._leash_xy, self._leash_z):
                sp.setDecimals(3)
                sp.setSingleStep(0.5)
            self._leash_xy.setValue(xy_steps * xy_cal)
            self._leash_z.setValue(z_steps * z_cal)
        else:
            self._leash_xy_lbl.setText("XY ± steps")
            self._leash_z_lbl.setText("Z ± steps")
            for sp in (self._leash_xy, self._leash_z):
                sp.setDecimals(0)
                sp.setSingleStep(1000)
            self._leash_xy.setValue(xy_steps)
            self._leash_z.setValue(z_steps)

    def _apply_leash(self) -> None:
        enabled = self._leash_on.isChecked()
        if self._leash_is_um():
            xy_cal, z_cal = self._leash_cal()
            xy = int(round(self._leash_xy.value() / xy_cal))
            z = int(round(self._leash_z.value() / z_cal))
        else:
            xy = int(round(self._leash_xy.value()))
            z = int(round(self._leash_z.value()))
        self._do(lambda: self.ctrl.set_leash(enabled=enabled, leash_xy=xy, leash_z=z))
        # keep the local cfg mirror in step (settings dialog / indicator fallback)
        self.cfg.limits.leash_enabled = enabled
        self.cfg.limits.leash_xy = xy
        self.cfg.limits.leash_z = z
        # re-show the (round-tripped) stored range in the current unit
        self._sync_leash_units()

    def _sync_leash_fields(self) -> None:
        self._leash_on.setChecked(self.cfg.limits.leash_enabled)
        self._sync_leash_units()

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
        path, _ = QFileDialog.getSaveFileName(self, "Save position list", "positions.json", "JSON (*.json)")
        if path:
            self._do(lambda: self.ctrl.save_positions(path))

    def _load_positions(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load position list", "", "JSON (*.json)")
        if path:
            self._do(lambda: self.ctrl.load_positions(path))
            self._reload_positions()

    def _reload_positions(self) -> None:
        try:
            positions = self.ctrl.get_positions()
        except Exception:
            positions = []
        self._table.setRowCount(len(positions))
        for i, p in enumerate(positions):
            used = p.get("used", False)
            cells = [
                str(i),
                p.get("name", "") if used else "—",
                f"{int(p.get('x', 0))}" if used else "",
                f"{int(p.get('y', 0))}" if used else "",
                f"{int(p.get('z', 0))}" if used else "",
            ]
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
        # what the service converts µm with, so this GUI converts the same way
        self._cal_live = (list(getattr(st, "um_per_step_fwd", st.um_per_step)),
                          list(getattr(st, "um_per_step_bwd", st.um_per_step)),
                          list(st.um_per_step))
        self._update_cal_hint(st)
        src = list(getattr(st, "um_per_step_src", ["config"] * 3))
        if src != getattr(self, "_cal_src", None):    # only on a real change
            self._cal_src = src
            self._sync_step_size_boxes()
        for a in range(3):
            um = st.position_um[a]
            self._big[a].setText(f"{um:.3f}")
            steps = st.position_steps[a]
            rel_um = st.rel_um[a]
            self._sub_lbl[a].setText(f"{int(steps)} steps · rel {rel_um:+.3f} µm")
        self._indicator.set_state(
            st.position_steps, st.moving, st.limit_lo, st.limit_hi, st.leash
        )
        if st.leash:
            xy_um = st.leash_half[0] * st.um_per_step[0]
            z_um = st.leash_half[2] * st.um_per_step[2]
            self._leash_hint.setText(
                f"ARMED — XY ±{int(st.leash_half[0])} steps (≈{xy_um:.1f} µm), "
                f"Z ±{int(st.leash_half[2])} steps (≈{z_um:.1f} µm), measured from the datum."
            )
        else:
            self._leash_hint.setText(
                "off — full travel limits apply. Tip: press Datum at a safe spot, "
                "set the ranges, then arm."
            )
        # reflect the preset state (e.g. if a coordinator changed it) without
        # re-triggering the toggle handlers; only when it actually changed
        if (st.speed_fast, st.step_large) != self._preset_state:
            self._preset_state = (st.speed_fast, st.step_large)
            self._sync_preset_buttons(st.speed_fast, st.step_large)
        # camera calibration: live progress; reload the table when it finishes
        running = bool(getattr(st, "calib_running", False))
        self._cal_state.setText(("RUNNING — " if running else "") + getattr(st, "calib_progress", ""))
        self._cal_start.setEnabled(not running)
        seen = (bool(getattr(st, "px_calibrated", False)), running)
        if seen != self._cal_seen:
            self._cal_seen = seen
            if not running:
                self._reload_calibration()

    def _on_event(self, level: str, msg: str) -> None:
        color = {"info": theme.COLORS["muted"], "warn": theme.COLORS["accent_hi"], "error": theme.COLORS["danger"]}.get(level, theme.COLORS["text"])
        self._log.appendHtml(f'<span style="color:{color}">[{level}]</span> {msg}')


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run_app(ctrl, cfg: Config, remote: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    from PySide6.QtCore import QLocale

    app = QApplication.instance() or QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from .theme import apply_window_icon
    apply_window_icon(app)
    # '.' decimal point, no thousands separator, whatever the Windows locale
    # (docs/DEVELOPER_NOTES.md gotcha #18): a step size of 0.02 µm showed as "0,02000",
    # and a leash of 1000000 steps as "1.000.000".
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
