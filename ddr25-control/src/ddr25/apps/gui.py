"""The front panel (section 7 of the guide): MainWindow + the signature dial.

PySide6, Fusion style, dark/light theme. ``run_app(ctrl, cfg, remote=False)``
builds the window; ``ctrl`` is either a local :class:`Rotator` brain or a
:class:`Ddr25Client` -- they share a method surface, so the GUI is agnostic.

Layout: LEFT = the big angle readout and the rotary dial (the stage seen from
above); RIGHT = move / jog / home / zero, the motion profile, the stored
angles and the log. Everything else (limits, wrap default, hardware) lives in
the Settings... window.

Threading rule: instrument events arrive on a background thread, so they MUST
cross into Qt through a signal -- see :class:`Bridge`.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QLocale, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import WRAP_POLICIES, Config
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


def _spin(value=0.0, lo=-1e6, hi=1e6, step=1.0, decimals=4, suffix="") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    # C locale: on a Finnish/Czech Windows the default would show "45,0000"
    # and a thousands separator (gotcha #18).
    loc = QLocale.c()
    loc.setNumberOptions(QLocale.OmitGroupSeparator)
    s.setLocale(loc)
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    s.setButtonSymbols(QDoubleSpinBox.NoButtons)
    s.setMinimumWidth(90)
    return s


def _fmt(v, digits=4, unit=" deg") -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "--"
    return f"{v:.{digits}f}{unit}"


# --------------------------------------------------------------------------- #
# signature indicator: the rotary dial
# --------------------------------------------------------------------------- #
class RotaryDial(QWidget):
    """The stage seen from above: a graduated ring and the rotating platter.

    * the platter carries a pointer at the current angle (0 deg at the top,
      angles grow CLOCKWISE, as a protractor held face-up over the stage);
    * a hollow triangle on the ring marks the target, and while the stage
      moves an amber arc runs from the pointer to it along the way the stage
      will actually turn (so the wrap policy is visible: shortest vs literal);
    * the arc's dashes march on their own ~33 ms timer, independent of the
      status poll, so motion reads as motion even between status frames;
    * NOT HOMED draws the ring dashed in the danger colour -- the angle is
      then relative to wherever the controller was switched on;
    * in literal mode the centre shows the turn count (720 deg = turn 2).
    Colours are read from theme.COLORS at paint time, never cached.
    """

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 300)
        self._angle = float("nan")
        self._target = None
        self._raw_to_go = 0.0      # signed degrees still to turn (sets arc direction)
        self._moving = False
        self._homed = False
        self._homing = False
        self._wrap = "literal"
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, angle, target, raw_to_go, moving, homed, homing, wrap) -> None:
        self._angle = angle
        self._target = target
        self._raw_to_go = raw_to_go
        self._moving = bool(moving)
        self._homed = bool(homed)
        self._homing = bool(homing)
        self._wrap = wrap
        if self._moving:
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 1.5) % 1000.0
        self.update()

    @staticmethod
    def _pt(cx, cy, r, deg) -> QPointF:
        # 0 deg at the top, clockwise: screen x = sin, screen y = -cos
        a = math.radians(deg)
        return QPointF(cx + r * math.sin(a), cy - r * math.cos(a))

    def paintEvent(self, _event) -> None:
        C = theme.COLORS
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side = min(w, h) - 24
        cx, cy = w / 2.0, h / 2.0
        R = side / 2.0                   # outer edge of the graduated ring
        r_plat = R * 0.72                # the rotating platter
        r_bore = R * 0.22                # the clear aperture (SM05 bore)

        # -- the housing (the fixed part) ------------------------------- #
        p.setPen(QPen(QColor(C["border"]), 1.5))
        p.setBrush(QColor(C["panel_hi"]))
        p.drawEllipse(QPointF(cx, cy), R, R)

        # graduation: minor every 5, major every 30, labels every 90
        for d in range(0, 360, 5):
            major = d % 30 == 0
            r0 = R - (R * 0.11 if major else R * 0.05)
            col = QColor(C["text"] if major else C["muted"])
            p.setPen(QPen(col, 2.0 if major else 1.0))
            p.drawLine(self._pt(cx, cy, r0, d), self._pt(cx, cy, R - 3, d))
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, R * 0.055))
        p.setFont(f)
        p.setPen(QColor(C["muted"]))
        for d in (0, 90, 180, 270):
            c = self._pt(cx, cy, R * 0.80, d)
            p.drawText(QRectF(c.x() - 20, c.y() - 9, 40, 18), Qt.AlignCenter, str(d))

        # NOT HOMED: the ring goes dashed red -- the numbers are not trustworthy
        if not self._homed:
            pen = QPen(QColor(C["danger"]), 2.0, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), R + 5, R + 5)

        ang = self._angle if (self._angle is not None and math.isfinite(self._angle)) else None

        # -- the platter (rotates with the stage) ----------------------- #
        p.setPen(QPen(QColor(C["border"]), 1.5))
        p.setBrush(QColor(C["panel"]))
        p.drawEllipse(QPointF(cx, cy), r_plat, r_plat)
        # four mounting holes that turn with it, so rotation is visible even
        # where the pointer is not
        if ang is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["grid"]))
            for k in range(4):
                hc = self._pt(cx, cy, r_plat * 0.62, ang + 45 + 90 * k)
                p.drawEllipse(hc, r_plat * 0.06, r_plat * 0.06)
        # the bore
        p.setPen(QPen(QColor(C["border"]), 1.0))
        p.setBrush(QColor(C["code_bg"]))
        p.drawEllipse(QPointF(cx, cy), r_bore, r_bore)

        # -- motion arc from the pointer to the target ------------------ #
        if self._moving and ang is not None and self._raw_to_go:
            span = max(min(self._raw_to_go, 3600.0), -3600.0)
            arc_r = R * 0.90
            rect = QRectF(cx - arc_r, cy - arc_r, 2 * arc_r, 2 * arc_r)
            # Qt: 0 deg at 3 o'clock, counter-clockwise positive, 1/16 deg units
            start_qt = 90.0 - ang
            span_qt = -min(abs(span), 359.0) * (1 if span > 0 else -1)
            pen = QPen(QColor(C["accent"]), 4.0, Qt.CustomDashLine)
            pen.setDashPattern([3, 3])
            pen.setDashOffset(-self._phase if span > 0 else self._phase)
            pen.setCapStyle(Qt.FlatCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawArc(rect, int(start_qt * 16), int(span_qt * 16))

        # -- target marker ---------------------------------------------- #
        if self._target is not None and math.isfinite(self._target):
            tip = self._pt(cx, cy, R + 2, self._target)
            left = self._pt(cx, cy, R + 14, self._target - 3.5)
            right = self._pt(cx, cy, R + 14, self._target + 3.5)
            p.setPen(QPen(QColor(C["accent_hi"]), 2.0))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(QPolygonF([tip, left, right]))

        # -- pointer ------------------------------------------------------ #
        if ang is not None:
            col = QColor(C["accent"] if self._moving else C["accent_hi"])
            if self._moving:
                glow = QColor(col)
                glow.setAlpha(60)
                p.setPen(QPen(glow, 10.0, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(self._pt(cx, cy, r_bore + 4, ang), self._pt(cx, cy, R - 6, ang))
            p.setPen(QPen(col, 3.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(self._pt(cx, cy, r_bore + 4, ang), self._pt(cx, cy, R - 6, ang))
            # the index notch on the platter rim
            path = QPainterPath()
            path.addEllipse(self._pt(cx, cy, r_plat - 8, ang), 5, 5)
            p.fillPath(path, col)

        # -- centre text: turn count / state ------------------------------ #
        f2 = QFont(self.font())
        f2.setPointSizeF(max(7.0, R * 0.05))
        f2.setBold(True)
        p.setFont(f2)
        if self._homing:
            txt, colr = "HOMING", C["accent"]
        elif not self._homed:
            txt, colr = "NOT\nHOMED", C["danger"]
        elif self._wrap == "literal" and ang is not None:
            turns = math.floor(ang / 360.0)
            txt, colr = (f"turn {turns:+d}" if turns else "turn 0"), C["muted"]
        else:
            txt, colr = "mod 360", C["muted"]
        p.setPen(QColor(colr))
        p.drawText(QRectF(cx - r_bore, cy - r_bore, 2 * r_bore, 2 * r_bore),
                   Qt.AlignCenter, txt)
        p.end()


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class MainWindow(QWidget):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self.remote = remote
        self.setWindowTitle("DDR25 Rotation Stage" + ("  [remote]" if remote else ""))
        self.resize(1180, 780)

        self._bridge = Bridge()
        self._bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda level, msg: self._bridge.event.emit(level, msg)

        self._build_ui()

        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()
        self._reload_angles()
        self._refresh()
        try:
            who = self.ctrl.info()["idn"] if remote else self.ctrl.idn()
        except Exception as exc:
            who = f"? ({exc})"
        self._on_event("info", f"connected: {who}")
        if not self.ctrl.status().homed:
            self._on_event("warn", "not homed: Home once before absolute moves")

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("DDR25 ROTATION STAGE")
        title.setObjectName("cardTitle")
        top.addWidget(title)
        self._state_lbl = QLabel("")
        self._state_lbl.setObjectName("muted")
        top.addWidget(self._state_lbl)
        top.addStretch(1)
        settings_btn = QPushButton("Settings...")
        settings_btn.clicked.connect(self._open_settings)
        top.addWidget(settings_btn)
        root.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.addWidget(self._build_readout_card(), 1)
        body.addLayout(left, 5)

        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(self._build_move_card())
        right.addWidget(self._build_profile_card())
        right.addWidget(self._build_angles_card(), 1)
        right.addWidget(self._build_log_card(), 1)
        body.addLayout(right, 6)
        root.addLayout(body, 1)

    def _build_readout_card(self) -> QFrame:
        frame, lay = _card("ANGLE")
        self._big = QLabel("--")
        self._big.setObjectName("bigValue")
        lay.addWidget(self._big)
        self._sub = QLabel("")
        self._sub.setObjectName("muted")
        lay.addWidget(self._sub)
        self._dial = RotaryDial()
        lay.addWidget(self._dial, 1)
        return frame

    def _build_move_card(self) -> QFrame:
        frame, lay = _card("MOVE  /  JOG  /  HOME  /  ZERO")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        grid.addWidget(QLabel("angle"), 0, 0)
        self._target_in = _spin(0.0, -1e6, 1e6, 1.0, 4, " deg")
        grid.addWidget(self._target_in, 0, 1)
        go = QPushButton("Go")
        go.setObjectName("primary")
        repolish(go)
        go.clicked.connect(lambda: self._do(lambda: self.ctrl.move_to(self._target_in.value())))
        grid.addWidget(go, 0, 2)
        grid.addWidget(QLabel("wrap"), 0, 3)
        self._wrap = QComboBox()
        self._wrap.addItems(list(WRAP_POLICIES))
        self._wrap.setToolTip("literal: a linear coordinate (safe with cables)\n"
                              "shortest / positive / negative: modulo 360")
        self._wrap.activated.connect(
            lambda _i: self._do(lambda: self.ctrl.set_wrap(self._wrap.currentText())))
        grid.addWidget(self._wrap, 0, 4)

        grid.addWidget(QLabel("step"), 1, 0)
        self._step = _spin(self.cfg.motion.jog_step, 0.0, 3600.0, 1.0, 3, " deg")
        grid.addWidget(self._step, 1, 1)
        jog = QHBoxLayout()
        minus = QPushButton("-  CCW")
        plus = QPushButton("CW  +")
        minus.clicked.connect(lambda: self._do(lambda: self.ctrl.move_by(-self._step.value())))
        plus.clicked.connect(lambda: self._do(lambda: self.ctrl.move_by(self._step.value())))
        jog.addWidget(minus)
        jog.addWidget(plus)
        grid.addLayout(jog, 1, 2, 1, 3)
        lay.addLayout(grid)

        row = QHBoxLayout()
        home = QPushButton("Home")
        home.setToolTip("Turn to the encoder index (up to one revolution)")
        home.clicked.connect(lambda: self._do(self.ctrl.home))
        zero = QPushButton("Zero here")
        zero.clicked.connect(lambda: self._do(self.ctrl.set_zero))
        clear = QPushButton("Clear zero")
        clear.clicked.connect(lambda: self._do(self.ctrl.clear_zero))
        stop = QPushButton("STOP")
        stop.setObjectName("danger")
        repolish(stop)
        stop.clicked.connect(lambda: self._do(lambda: self.ctrl.stop(False)))
        for b in (home, zero, clear):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(stop)
        lay.addLayout(row)
        return frame

    def _build_profile_card(self) -> QFrame:
        frame, lay = _card("MOTION PROFILE")
        row = QHBoxLayout()
        row.addWidget(QLabel("velocity"))
        self._vel_in = _spin(self.cfg.motion.velocity, 0.1, 1e5, 10.0, 2, " deg/s")
        row.addWidget(self._vel_in)
        setv = QPushButton("Set")
        setv.clicked.connect(lambda: self._do(lambda: self.ctrl.set_velocity(self._vel_in.value())))
        row.addWidget(setv)
        row.addSpacing(10)
        row.addWidget(QLabel("accel"))
        self._acc_in = _spin(self.cfg.motion.acceleration, 1.0, 1e6, 50.0, 1, " deg/s2")
        row.addWidget(self._acc_in)
        seta = QPushButton("Set")
        seta.clicked.connect(lambda: self._do(lambda: self.ctrl.set_acceleration(self._acc_in.value())))
        row.addWidget(seta)
        lay.addLayout(row)
        self._prof_lbl = QLabel("")
        self._prof_lbl.setObjectName("muted")
        lay.addWidget(self._prof_lbl)
        return frame

    def _build_angles_card(self) -> QFrame:
        frame, lay = _card("STORED ANGLES")
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["#", "name", "angle"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setMinimumHeight(120)
        lay.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._slot_name = QLineEdit()
        self._slot_name.setPlaceholderText("name for the selected slot")
        store = QPushButton("Store here")
        store.setObjectName("primary")
        repolish(store)
        goto = QPushButton("Go to")
        clear = QPushButton("Clear")
        store.clicked.connect(self._store_current)
        goto.clicked.connect(lambda: self._do(lambda: self.ctrl.goto_angle(self._selected_slot())))
        clear.clicked.connect(self._clear_selected)
        row.addWidget(self._slot_name, 1)
        for b in (store, goto, clear):
            row.addWidget(b)
        lay.addLayout(row)

        frow = QHBoxLayout()
        frow.addStretch(1)
        save = QPushButton("Save list...")
        load = QPushButton("Load list...")
        save.clicked.connect(self._save_angles)
        load.clicked.connect(self._load_angles)
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
        self._log.setMinimumHeight(90)
        lay.addWidget(self._log)
        return frame

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #
    def _open_settings(self) -> None:
        if self.remote:
            # Re-read the SERVICE's config first: the display zero or the
            # velocity may have been changed from elsewhere since we connected,
            # and pushing our stale copy back would undo that (gotcha #5).
            try:
                from ..net.protocol import apply_config_dict
                apply_config_dict(self.cfg, self.ctrl.get_config())
            except Exception as exc:
                self._on_event("warn", f"could not refresh config: {exc}")
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
    def _do(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self._on_event("error", f"{type(exc).__name__}: {exc}")

    def _selected_slot(self) -> int:
        row = self._table.currentRow()
        return row if row >= 0 else 0

    def _store_current(self) -> None:
        slot = self._selected_slot()
        name = self._slot_name.text().strip()
        self._do(lambda: self.ctrl.store_angle(slot, name))
        self._reload_angles()

    def _clear_selected(self) -> None:
        self._do(lambda: self.ctrl.clear_angle(self._selected_slot()))
        self._reload_angles()

    def _save_angles(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save stored angles", "angles.json", "JSON (*.json)")
        if path:
            self._do(lambda: self.ctrl.save_angles(path))

    def _load_angles(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load stored angles", "", "JSON (*.json)")
        if path:
            self._do(lambda: self.ctrl.load_angles(path))
            self._reload_angles()

    def _reload_angles(self) -> None:
        try:
            slots = self.ctrl.get_angles()
        except Exception:
            slots = []
        zero = self.ctrl.status().zero_deg
        self._table.setRowCount(len(slots))
        for i, s in enumerate(slots):
            used = s.get("used", False)
            ang = s.get("raw", 0.0) - (zero if math.isfinite(zero) else 0.0)
            cells = [str(i), s.get("name", "") if used else "--",
                     f"{ang:.4f}" if used else ""]
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
        self._big.setText(_fmt(st.angle_deg))
        self._sub.setText(
            f"controller {_fmt(st.raw_deg)}   |   target {_fmt(st.target_deg)}   |   "
            f"zero {_fmt(st.zero_deg)}")
        bits = []
        bits.append("HOMING" if st.homing else ("homed" if st.homed else "NOT HOMED"))
        bits.append("moving" if st.moving else "idle")
        bits.append(f"wrap {st.wrap}")
        if st.streaming:
            bits.append("streaming")
        if st.hw_error:
            bits.append(f"ERROR {st.hw_error}")
        if not st.connected:
            bits.append("disconnected")
        self._state_lbl.setText("  -  ".join(bits))
        self._prof_lbl.setText(f"controller reports {_fmt(st.velocity, 2, ' deg/s')}, "
                               f"{_fmt(st.acceleration, 1, ' deg/s2')}")
        if self._wrap.currentText() != st.wrap and not self._wrap.view().isVisible():
            self._wrap.blockSignals(True)          # gotcha #13: no feedback loop
            self._wrap.setCurrentText(st.wrap)
            self._wrap.blockSignals(False)

        # the arc: how far the CONTROLLER still has to turn (sign = direction)
        to_go = 0.0
        if st.moving and st.target_deg is not None and math.isfinite(st.raw_deg):
            target_raw = self._target_raw(st)
            if target_raw is not None:
                to_go = target_raw - st.raw_deg
        self._dial.set_state(st.angle_deg, self._shown_target(st), to_go,
                             st.moving, st.homed, st.homing, st.wrap)

    @staticmethod
    def _shown_target(st):
        t = st.target_deg
        if t is None:
            return None
        return t if st.wrap == "literal" else t % 360.0

    @staticmethod
    def _target_raw(st):
        """Rebuild the controller target for the arc: literal is zero + angle;
        in a modulo mode the direction follows the policy."""
        from ..angles import raw_target
        try:
            # while moving, the remaining turn is the same computation the
            # brain did, but from where the stage is NOW
            return raw_target(st.wrap, st.raw_deg, st.zero_deg, st.target_deg)
        except Exception:
            return None

    def _on_event(self, level: str, msg: str) -> None:
        color = {"info": theme.COLORS["muted"], "warn": theme.COLORS["accent_hi"],
                 "error": theme.COLORS["danger"]}.get(level, theme.COLORS["text"])
        self._log.appendHtml(f'<span style="color:{color}">[{level}]</span> {msg}')


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def run_app(ctrl, cfg: Config, remote: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    # Choose the palette BEFORE any widget is built, so every widget and custom
    # paintEvent reads the right COLORS. Startup-only: there is no live toggle.
    theme.set_theme(getattr(cfg.ui, "theme", "dark"))

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
    """A DEMO pose against the simulator (used by tools/render_all.py): home,
    store two angles, then leave the stage part-way through a slow move so the
    panel shows the dial's motion arc. For real use run scripts/run_gui.py."""
    import time

    from ..sim_system import build_sim_system

    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain.set_velocity(360.0)
    brain.set_acceleration(3000.0)
    brain.home()
    t0 = time.monotonic()
    time.sleep(0.05)
    while brain.status().moving and time.monotonic() - t0 < 5.0:
        time.sleep(0.02)
    for slot, (ang, name) in enumerate(((0.0, "index"), (45.0, "s-pol"), (135.0, "p-pol"))):
        brain.move_to(ang)
        time.sleep(0.05)
        while brain.status().moving and time.monotonic() - t0 < 10.0:
            time.sleep(0.02)
        brain.store_angle(slot, name)
    brain.set_velocity(40.0)
    brain.set_acceleration(200.0)
    brain.move_to(300.0)
    try:
        return run_app(brain, cfg)
    finally:
        brain.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
