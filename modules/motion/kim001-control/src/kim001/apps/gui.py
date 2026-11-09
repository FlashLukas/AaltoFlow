"""The front panel (section 7 of the guide): MainWindow + the signature indicator.

PySide6, Fusion style, dark/amber theme.  ``run_app(ctrl, cfg, remote=False)``
builds the window; ``ctrl`` is either a local :class:`Kim` brain or a
:class:`Kim001Client` -- they share a method surface, so the GUI is agnostic.

ONE AXIS ON SCREEN. A KIM001 drives a single PIA13 that moves the microscope
objective (focus). The brain keeps kim-control's [X, Y, Z] shape for the
camera's sake (see config.py), but X and Y are not fitted, so this panel
simply does not show them: no greyed rows to wonder about, only the focus
axis Z (``FITTED_AXES``). Widgets are kept in dicts keyed by that axis index.

The panel is built around the module's idea of TWO LANGUAGES:
  * a MOVE card that commands absolute/relative targets in either STEPS or
    MICROMETRES (a unit selector + a "relative (from here)" toggle),
  * a STEP SIZE card for the micrometre bridge (um per step, per direction),
    and motion presets for the native drive knobs (rate, voltage).

Threading rule: instrument events arrive on a background thread, so they MUST
cross into Qt through a signal -- see :class:`Bridge`.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import AXES, FITTED_AXES, Config
from . import theme
from .control_bar import ControlBar, mark_always
from .settings_dialog import SettingsDialog
from .theme import repolish

#: The focus axis (index 2 = Z): the only one this panel shows.
Z = FITTED_AXES[0]


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
class FocusIndicator(QWidget):
    """Side view of the focus axis: an objective riding up and down its travel.

    Left: a vertical travel bar drawn SYMMETRIC about the datum (step 0 is the
    centre line) -- the envelope is the effective limit, i.e. the leash box
    when it is armed. Right: an objective (barrel + front lens) at the height
    of the live step position, above a dashed "sample" line. While the axis
    moves, the front lens emits expanding SQUARE rings -- kim-control's
    "ratchety" motif for discrete slip-stick steps (the closed-loop stages
    use smooth round halos). Its own ~33 ms QTimer drives the animation,
    independent of the status poll.

    ``set_state`` takes kim's 3-wide lists and uses index Z only.
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setMinimumHeight(220)
        self._pos = 0
        self._moving = False
        self._lo, self._hi = -1, 1
        self._leash = False
        self._phase = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, pos, moving, lo=None, hi=None, leash=False) -> None:
        self._pos = int(pos[Z])
        self._moving = bool(moving[Z])
        if lo is not None:
            self._lo = int(lo[Z])
        if hi is not None:
            self._hi = int(hi[Z])
        self._leash = bool(leash)
        if self._moving:
            if not self._timer.isActive():
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()
            self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.10) % 1.0
        self.update()

    def _half(self) -> float:
        """Symmetric half-span used for display: the axis is drawn as
        [-half, +half] so the datum (step 0) always sits at the CENTRE."""
        return max(abs(self._lo), abs(self._hi), 1.0)

    def _frac(self) -> float:
        """Map the step position onto 0..1 with 0 pinned to the centre (0.5)."""
        return min(1.0, max(0.0, 0.5 + self._pos / (2.0 * self._half())))

    def paintEvent(self, _event) -> None:
        C = theme.COLORS
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        margin = 16
        top, bottom = margin + 8, h - margin - 8
        span = bottom - top

        # amber border while the leash is arming the travel box
        border_col = QColor(C["accent"]) if self._leash else QColor(C["border"])
        p.setPen(QPen(border_col, 1))
        p.setBrush(QColor(C["code_bg"]))
        p.drawRoundedRect(margin, margin, w - 2 * margin, h - 2 * margin, 8, 8)

        # --- travel bar (left) ---------------------------------------------
        bar_x, bar_w = margin + 18, 26
        p.setPen(QPen(border_col, 1))
        p.setBrush(QColor(C["panel_hi"]))
        p.drawRoundedRect(bar_x, top, bar_w, span, 6, 6)
        zc = top + span / 2.0
        level = top + (1.0 - self._frac()) * span
        lo_y, hi_y = min(zc, level), max(zc, level)
        if hi_y - lo_y < 1:
            hi_y = lo_y + 1
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(C["accent"]) if self._moving else QColor(C["ok"]))
        p.drawRoundedRect(bar_x + 4, int(lo_y), bar_w - 8, int(hi_y - lo_y), 3, 3)
        p.setPen(QPen(QColor(C["accent_dim"]), 1, Qt.DashLine))
        p.drawLine(bar_x - 6, int(zc), bar_x + bar_w + 6, int(zc))
        p.setPen(QColor(C["muted"]))
        p.drawText(bar_x + bar_w + 8, int(zc) + 4, "datum")
        # beside the bar, not above it: above, it ran into the frame line
        p.drawText(bar_x + bar_w + 8, top + 12, "+Z")

        # leash / travel tag, top right
        tag = (f"LEASH ±{int(self._half())} steps" if self._leash
               else f"full travel ±{int(self._half())} steps")
        p.setPen(QColor(C["accent_hi"]) if self._leash else QColor(C["muted"]))
        fm = p.fontMetrics()
        p.drawText(w - margin - 10 - fm.horizontalAdvance(tag), margin + 18, tag)

        # --- objective (right) ----------------------------------------------
        ox = bar_x + bar_w + 70 + (w - bar_x - bar_w - 70 - margin) / 2.0
        ox = min(ox, w - margin - 60)
        barrel_w, barrel_h = 44.0, 46.0
        lens_w = 22.0
        sample_y = bottom - 4
        # the objective's front lens rides between the top and just above the
        # sample line; its height follows the step position
        travel_top, travel_bot = top + barrel_h + 10, sample_y - 22
        tip = travel_top + (1.0 - self._frac()) * (travel_bot - travel_top)

        # sample plane
        p.setPen(QPen(QColor(C["muted"]), 2, Qt.DashLine))
        p.drawLine(int(ox - 70), int(sample_y), int(ox + 70), int(sample_y))
        p.setPen(QColor(C["muted"]))
        p.drawText(int(ox + 74), int(sample_y) + 4, "sample")

        base = QColor(C["accent"]) if self._moving else QColor(C["accent_dim"])
        # barrel (rectangle) + tapered nose (trapezoid) down to the front lens
        p.setPen(QPen(QColor(C["accent_hi"]), 2))
        p.setBrush(QColor(C["panel_hi"]))
        nose_h = 14.0
        p.drawRect(int(ox - barrel_w / 2), int(tip - nose_h - barrel_h),
                   int(barrel_w), int(barrel_h))
        nose = QPolygonF([QPointF(ox - barrel_w / 2, tip - nose_h),
                          QPointF(ox + barrel_w / 2, tip - nose_h),
                          QPointF(ox + lens_w / 2, tip),
                          QPointF(ox - lens_w / 2, tip)])
        p.setBrush(base)
        p.drawPolygon(nose)
        # a ring band on the barrel (the "objective" look)
        p.setPen(QPen(base, 3))
        band_y = tip - nose_h - barrel_h * 0.35
        p.drawLine(int(ox - barrel_w / 2 + 3), int(band_y), int(ox + barrel_w / 2 - 3), int(band_y))

        # discrete "step" rings around the front lens while moving
        if self._moving:
            for k in (0, 1):
                t = (self._phase + 0.5 * k) % 1.0
                side = 8 + 30 * t
                col = QColor(C["accent"])
                col.setAlpha(int(150 * (1.0 - t)))
                p.setPen(QPen(col, 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(int(ox - side / 2), int(tip + 2), int(side), int(side / 2))

        # working-distance tick from the lens to the sample line
        p.setPen(QPen(QColor(C["grid"]), 1, Qt.DotLine))
        p.drawLine(int(ox), int(tip + 2), int(ox), int(sample_y))
        p.end()


#: kim-control's name for the signature widget, kept so code written against
#: kim reads the same.
InertiaIndicator = FocusIndicator


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class MainWindow(QWidget):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self.remote = remote
        self.setWindowTitle("Inertia Focus (KIM001 / PIA13)" + ("  [remote]" if remote else ""))
        self.resize(760, 900)

        self._bridge = Bridge()
        self._bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda level, msg: self._bridge.event.emit(level, msg)

        self._build_ui()

        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()

        self._reload_positions()
        # The first GUI to connect gets control; a later one opens as a viewer
        # (control_bar.py). Only once the log exists, so the bar can say so.
        if self._control_bar is not None:
            self._control_bar.claim_if_free()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("INERTIA FOCUS  ·  KIM001 / PIA13  ·  objective Z")
        title.setObjectName("cardTitle")
        top.addWidget(title)
        top.addStretch(1)
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        top.addWidget(settings_btn)
        root.addLayout(top)
        # a viewer may LOOK at the settings; the service refuses an OK from it
        mark_always(settings_btn)

        # Control or viewer (control_bar.py), only for a GUI on a service
        # whose client knows about control; a local GUI owns its brain.
        self._control_bar = None
        if self.remote and hasattr(self.ctrl, "take_control"):
            self._control_bar = ControlBar(self.ctrl, self, log=self._on_event)
            root.addWidget(self._control_bar)

        # A failed hardware read, in red, above everything else. The position
        # below then repeats the LAST GOOD reading, so this line is what tells
        # you it is stale. Hidden while the link is fine.
        self._hw_error_lbl = QLabel("")
        self._hw_error_lbl.setWordWrap(True)
        self._hw_error_lbl.setStyleSheet(
            f"color:{theme.COLORS['danger']}; font-weight:700;")
        self._hw_error_lbl.hide()
        root.addWidget(self._hw_error_lbl)

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
        col.addWidget(self._build_positions_card(), 1)
        col.addWidget(self._build_log_card(), 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(controls)
        root.addWidget(scroll, 1)

        # match the leash box to the initial unit (default µm)
        self._sync_leash_units()

    def _build_readout_card(self) -> QFrame:
        frame, lay = _card("FOCUS POSITION  (µm · steps · rel)")
        row = QHBoxLayout()
        self._big: dict[int, QLabel] = {}
        self._sub_lbl: dict[int, QLabel] = {}
        for a in FITTED_AXES:
            c = QVBoxLayout()
            cap = QLabel(f"{AXES[a]}  (objective)")
            cap.setObjectName("caption")
            big = QLabel("--")
            big.setObjectName("bigValue")
            sub = QLabel("-- steps · rel --")
            sub.setObjectName("muted")
            c.addWidget(cap)
            c.addWidget(big)
            c.addWidget(sub)
            row.addLayout(c)
            self._big[a] = big
            self._sub_lbl[a] = sub
        row.addStretch(1)
        lay.addLayout(row)

        self._indicator = FocusIndicator(self.cfg)
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
        self._target: dict[int, QDoubleSpinBox] = {}
        self._jog_buttons: list = []
        for row, a in enumerate(FITTED_AXES):
            grid.addWidget(QLabel(AXES[a]), row, 0)
            sp = _spin(0.0, -1e9, 1e9, 1.0, 3)
            grid.addWidget(sp, row, 1)
            self._target[a] = sp

            move_btn = QPushButton("Move")
            move_btn.setMaximumWidth(58)
            move_btn.clicked.connect(lambda _c, ax=a: self._move(ax))
            grid.addWidget(move_btn, row, 2)

            # The ± buttons jog by the JOG STEP (bottom row), not by the value in
            # this row's box -- so they carry that amount on their face
            # (kim-control: a bare "−"/"+" was mistaken for "move by the box").
            minus = QPushButton("−")
            plus = QPushButton("+")
            minus.setMinimumWidth(74)
            plus.setMinimumWidth(74)
            minus.setToolTip("jog DOWN by the jog step (bottom row); the box on the left is for Move")
            plus.setToolTip("jog UP by the jog step (bottom row); the box on the left is for Move")
            self._jog_buttons.append((minus, plus))
            minus.clicked.connect(lambda _c, ax=a: self._jog(ax, -1))
            plus.clicked.connect(lambda _c, ax=a: self._jog(ax, +1))
            grid.addWidget(minus, row, 3)
            grid.addWidget(plus, row, 4)

            zero = QPushButton("Zero")
            zero.setMaximumWidth(60)
            zero.setToolTip("set the display origin here (read-out only)")
            zero.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.set_zero(ax)))
            grid.addWidget(zero, row, 5)

            datum = QPushButton("Datum")
            datum.setMaximumWidth(78)
            datum.setToolTip("reset the hardware step counter to 0 here -- "
                             "press it at a focus you trust, then arm the leash")
            datum.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.zero_counter(ax)))
            grid.addWidget(datum, row, 6)
        grid.setColumnStretch(7, 1)
        lay.addLayout(grid)

        # keep the target box's precision honest: steps are integers (0 dp),
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
        stop = QPushButton("STOP")
        stop.setObjectName("danger")
        repolish(stop)
        stop.clicked.connect(lambda: self._do(self.ctrl.stop_all))
        row.addWidget(stop)
        mark_always(stop)            # a viewer can always stop the actuator
        lay.addLayout(row)
        # Which step size the µm boxes use: one per direction, which differ on
        # a slip-stick actuator -- say so rather than let a µm jog look exact.
        self._cal_hint = QLabel("")
        self._cal_hint.setObjectName("hint")
        self._cal_hint.setWordWrap(True)
        lay.addWidget(self._cal_hint)
        return frame

    def _build_leash_card(self) -> QFrame:
        frame, lay = _card("LEASH  (limit focus travel to a box around the Datum)")
        row = QHBoxLayout()
        self._leash_on = QCheckBox("armed")
        self._leash_on.setToolTip(
            "When armed, Z may only move ± the range below, measured from the "
            "Datum. Press Datum at a focus you trust first: then the objective "
            "cannot be driven further than this from it (e.g. into the sample)."
        )
        row.addWidget(self._leash_on)
        row.addSpacing(12)
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
        """Type the step size in by hand, per direction.

        Forward and backward are separate boxes because a slip-stick actuator
        does not step equally both ways; leave backward at 0 to say "same as
        forward" (which is all a datasheet tells you). The camera's "Calibrate
        Z steps" fills both through set_calibration and saves them itself.
        """
        frame, lay = _card("STEP SIZE  (µm per step · up / down)")
        grid = QGridLayout()
        grid.addWidget(QLabel("forward (up)"), 0, 1)
        grid.addWidget(QLabel("backward (down, 0 = same)"), 0, 2)
        self._cal_boxes: dict[int, tuple] = {}
        for row, a in enumerate(FITTED_AXES, start=1):
            grid.addWidget(QLabel(AXES[a]), row, 0)
            fwd = _spin(0.02, 1e-6, 1000.0, 0.001, 5)
            bwd = _spin(0.0, 0.0, 1000.0, 0.001, 5)
            bwd.setSpecialValueText("same")     # 0 reads as "same", not "0"
            grid.addWidget(fwd, row, 1)
            grid.addWidget(bwd, row, 2)
            self._cal_boxes[a] = (fwd, bwd)
        grid.setColumnStretch(3, 1)
        buttons = QHBoxLayout()
        apply = QPushButton("Apply step sizes")
        apply.setObjectName("primary")
        apply.clicked.connect(self._apply_step_sizes)
        # Save = write the step sizes in force into kim001.ini (on the
        # service's PC), so a restart keeps them. Apply alone only changes the
        # running service.
        self._cal_save_btn = QPushButton("Save")
        self._cal_save_btn.setToolTip("Save the step sizes (both directions) in force to "
                                      "kim001.ini, loaded at the next start "
                                      "(typed numbers: press Apply first)")
        self._cal_save_btn.clicked.connect(self._save_step_sizes)
        buttons.addWidget(apply)
        buttons.addWidget(self._cal_save_btn)
        grid.addLayout(buttons, 1, 4)
        lay.addLayout(grid)
        self._cal_manual_hint = QLabel(
            "The camera's AutoFocus tab > Calibrate Z steps measures both directions "
            "from the image and stores them here. By hand: move N steps, measure the "
            "focus shift (a known feature, a dial gauge), divide by N, each direction "
            "separately.")
        self._cal_manual_hint.setObjectName("hint")
        self._cal_manual_hint.setWordWrap(True)
        lay.addWidget(self._cal_manual_hint)
        self._sync_step_size_boxes()
        return frame

    def _sync_step_size_boxes(self) -> None:
        """Show the numbers in force (not while the user is typing in one)."""
        c = self.cfg.calibration
        values = {2: (c.um_per_step_z, c.um_per_step_z_bwd)}
        live = getattr(self, "_cal_live", None)
        for a, (fwd, bwd) in self._cal_boxes.items():
            f, b = values.get(a, (0.02, 0.0))
            if live:
                # what the service USES: forward/backward; "same" when equal
                f = live[0][a]
                b = live[1][a] if abs(live[1][a] - live[0][a]) > 1e-12 else 0.0
            for box, v in ((fwd, f), (bwd, b)):
                if not box.hasFocus():
                    box.blockSignals(True)
                    box.setValue(v)
                    box.blockSignals(False)

    def _apply_step_sizes(self) -> None:
        for a, (fwd, bwd) in self._cal_boxes.items():
            b = bwd.value()
            if b > 0:
                self._do(lambda a=a, v=fwd.value(): self.ctrl.set_calibration(a, v, +1))
                self._do(lambda a=a, v=b: self.ctrl.set_calibration(a, v, -1))
            else:   # "same": one number again for this axis
                self._do(lambda a=a, v=fwd.value(): self.ctrl.set_calibration(a, v, 0))
        # a remote GUI edits the SERVICE's config, so pull it back to stay in step
        self._pull_config()
        self._cal_live = None          # show the new numbers until status catches up
        self._sync_step_size_boxes()

    def _save_step_sizes(self) -> None:
        # Saves what the brain/service HOLDS, not the boxes: typed numbers
        # count once Apply has sent them (the tooltip says so).
        self._do(lambda: self._on_event("info", "step sizes saved to "
                                                + self.ctrl.save_calibration()))

    def _build_presets_card(self) -> QFrame:
        frame, lay = _card("MOTION PRESETS")
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
        fast0 = abs(m.rate_z - m.fast_rate) <= abs(m.rate_z - m.slow_rate)
        v = m.voltage_z
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
            "Rate / accel / voltage in detail are in Settings."
        )

    def _sync_preset_buttons(self, fast: bool, large: bool) -> None:
        """Set the buttons to a state WITHOUT re-triggering the apply handlers."""
        self._suppress_presets = True
        self._speed_btn.setChecked(fast)
        self._steps_btn.setChecked(large)
        self._suppress_presets = False
        self._paint_preset_buttons(fast, large)

    def _build_positions_card(self) -> QFrame:
        frame, lay = _card("FOCUS POSITIONS  (20 slots, steps)")
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["#", "name", "Z (steps)"])
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
    def _pull_config(self) -> None:
        """Remote GUI: refresh the local config MIRROR from the service.

        The mirror is filled once at launch. Settings edits it and pushes ALL
        of it back with set_config, so anything changed on the service since
        (a step size from the camera's Z calibration, a leash set by a script)
        would be put back to its launch-time value (gotcha #5). A local GUI
        shares the brain's Config object, so there is nothing to do.
        """
        if not self.remote:
            return
        from ..net.protocol import apply_config_dict
        try:
            apply_config_dict(self.cfg, self.ctrl.get_config())
        except Exception as exc:
            self._on_event("warn", f"could not refresh the config from the service: {exc}")

    def _open_settings(self) -> None:
        self._pull_config()           # edit what the service has NOW, not at launch
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self._push_config()
            self._sync_leash_fields()
            if getattr(dlg, "save_requested", False):
                # "Save config": apply as OK does, THEN save -- so the file
                # holds what the service actually took, and it is saved where
                # the service lives (its kim001.ini), also from a remote GUI.
                self._do(lambda: self._on_event("info", "config saved to "
                                                        + self.ctrl.save_config()))
            # The presets are NOT re-applied here (kim-control, 2026-09-28): a
            # preset is written only when its button is clicked.
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

        The leash box follows the same unit (built after the move card, so
        guard until it exists)."""
        steps = text == "steps"
        for sp in self._target.values():
            val = sp.value()
            sp.setDecimals(0 if steps else 3)
            sp.setSingleStep(1.0 if steps else 0.1)
            if steps:
                sp.setValue(round(val))
        if hasattr(self, "_jog_step"):
            self._sync_jog_units()
        if hasattr(self, "_leash_z"):
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
        """µm per step as the SERVICE is using it (status), not as configured,
        so a step count sent from here matches the µm shown."""
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
        the physical jog distance."""
        um = self._unit.currentText() == "µm"
        cal = self._axis_cal(Z)
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
        """One line saying which step size the µm <-> steps conversion uses."""
        fwd = list(getattr(st, "um_per_step_fwd", st.um_per_step))
        bwd = list(getattr(st, "um_per_step_bwd", st.um_per_step))
        a = Z
        if abs(fwd[a] - bwd[a]) > 1e-4 * max(fwd[a], bwd[a]):
            size = f"{fwd[a] * 1000:.1f} nm up / {bwd[a] * 1000:.1f} nm down"
        else:
            size = f"{fwd[a] * 1000:.1f} nm both ways"
        self._cal_hint.setText(f"µm ↔ steps: {size} per step (STEP SIZE card)")

    def _label_jog_buttons(self) -> None:
        um = self._unit.currentText() == "µm"
        amount = f"{self._jog_step.value():g} µm" if um else f"{int(self._jog_step.value())} st"
        for minus, plus in self._jog_buttons:
            minus.setText(f"− {amount}")
            plus.setText(f"+ {amount}")

    def _jog(self, axis: int, sign: int) -> None:
        if self._unit.currentText() == "µm":
            # the direction decides the step size: +2 µm and -2 µm are NOT the
            # same number of steps on a slip-stick actuator
            delta = int(round(self._jog_step.value() / self._axis_cal(axis, sign)))
        else:
            delta = int(round(self._jog_step.value()))
        self._do(lambda: self.ctrl.move_steps(axis, sign * delta))

    # -- the leash box follows the MOVE card's unit (steps or µm) ----------- #
    def _leash_is_um(self) -> bool:
        return self._unit.currentText() == "µm"

    def _sync_leash_units(self) -> None:
        """Relabel + rescale the leash box for the current unit, showing the
        stored (authoritative, in steps) range converted to that unit with the
        step size IN FORCE (status)."""
        z_steps = self.cfg.limits.leash_z
        if self._leash_is_um():
            self._leash_z_lbl.setText("Z ± µm")
            self._leash_z.setDecimals(3)
            self._leash_z.setSingleStep(0.5)
            self._leash_z.setValue(z_steps * self._axis_cal(Z))
        else:
            self._leash_z_lbl.setText("Z ± steps")
            self._leash_z.setDecimals(0)
            self._leash_z.setSingleStep(1000)
            self._leash_z.setValue(z_steps)

    def _apply_leash(self) -> None:
        enabled = self._leash_on.isChecked()
        if self._leash_is_um():
            z = int(round(self._leash_z.value() / self._axis_cal(Z)))
        else:
            z = int(round(self._leash_z.value()))
        self._do(lambda: self.ctrl.set_leash(enabled=enabled, leash_z=z))
        # keep the local cfg mirror in step (settings dialog / indicator fallback)
        self.cfg.limits.leash_enabled = enabled
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
        if self._control_bar is not None:
            self._control_bar.refresh()
        st = self.ctrl.status()
        err = getattr(st, "hw_error", "") or ""
        if err:
            self._hw_error_lbl.setText(
                f"HARDWARE ERROR: {err} -- the position shows the last good reading")
            self._hw_error_lbl.show()
        elif not self._hw_error_lbl.isHidden():
            self._hw_error_lbl.hide()
        # what the service converts µm with, so this GUI converts the same way
        old_live = getattr(self, "_cal_live", None)
        self._cal_live = (list(getattr(st, "um_per_step_fwd", st.um_per_step)),
                          list(getattr(st, "um_per_step_bwd", st.um_per_step)),
                          list(st.um_per_step))
        if old_live != self._cal_live:
            # The leash box in µm was converted with the step size of the
            # moment; when it changes, re-show it, or Apply would convert back
            # with a different number and quietly resize the box.
            if not self._leash_z.hasFocus():
                self._sync_leash_units()
            self._sync_step_size_boxes()
        self._update_cal_hint(st)
        for a in FITTED_AXES:
            self._big[a].setText(f"{st.position_um[a]:.3f} µm")
            self._sub_lbl[a].setText(
                f"{int(st.position_steps[a])} steps · rel {st.rel_um[a]:+.3f} µm")
        self._indicator.set_state(
            st.position_steps, st.moving, st.limit_lo, st.limit_hi, st.leash
        )
        if st.leash:
            z_um = st.leash_half[Z] * st.um_per_step[Z]
            self._leash_hint.setText(
                f"ARMED — Z ±{int(st.leash_half[Z])} steps (≈{z_um:.1f} µm) from the datum."
            )
        else:
            self._leash_hint.setText(
                "off — full travel limits apply. Tip: press Datum at a focus you trust, "
                "set the range, then arm."
            )
        # reflect the preset state (e.g. if a coordinator changed it) without
        # re-triggering the toggle handlers; only when it actually changed
        if (st.speed_fast, st.step_large) != self._preset_state:
            self._preset_state = (st.speed_fast, st.step_large)
            self._sync_preset_buttons(st.speed_fast, st.step_large)

    def _on_event(self, level: str, msg: str) -> None:
        color = {"info": theme.COLORS["muted"], "warn": theme.COLORS["accent_hi"],
                 "error": theme.COLORS["danger"]}.get(level, theme.COLORS["text"])
        self._log.appendHtml(f'<span style="color:{color}">[{level}]</span> {msg}')


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def run_app(ctrl, cfg: Config, remote: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    from PySide6.QtCore import QLocale

    app = QApplication.instance() or QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
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


def main(theme: str = "dark") -> int:
    """A simulated panel with something to look at, for tools/render_panels.py.

    That tool has no hand-made target for this module and falls back to
    ``<package>.apps.gui.main(theme=...)``. So: build the simulator, start the
    brain (as scripts/run_gui.py does), arm a small leash around the datum and
    send the objective a little way up at the fast preset, so the picture
    shows a position, the leash box and the travel bar -- then run the app.
    (For a real window use scripts/run_gui.py.)
    """
    from ..sim_system import build_sim_system

    cfg = Config()
    cfg.ui.theme = theme
    brain, _ = build_sim_system(cfg)
    brain.start()
    try:
        brain.set_leash(enabled=True, leash_z=20000)     # +/- 400 um at 20 nm
        brain.set_speed(True)
        brain.move_to_step(Z, 3000)                       # 60 um up, ~2 s at fast
        return run_app(brain, cfg, remote=False)
    finally:
        brain.shutdown()
