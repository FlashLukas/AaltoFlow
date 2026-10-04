"""The front panel (section 7 of the guide): MainWindow + the signature indicator.

PySide6, Fusion style, dark/amber theme. ``run_app(ctrl, cfg, remote=False)``
builds the window; ``ctrl`` is either a local :class:`Positioner` brain or a
:class:`SmaractClient` -- they share a method surface, so the GUI is agnostic.

Layout:
  * a top bar with the reference badge and a Settings... button,
  * LEFT: the position read-out + the RailIndicator, the move card (go to,
    jog, find reference, zero, STOP) and the speed card,
  * RIGHT: the stored-position list and the log.

Threading rule: instrument events arrive on a background thread, so they MUST
cross into Qt through a signal -- see :class:`Bridge`.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QLocale, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
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
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..backends.sim import MARK_CODE_MM, MARK_PITCH_MM
from ..config import Config
from . import theme
from .control_bar import ControlBar, mark_always
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


def _c_locale() -> QLocale:
    """The C locale without group separators: on the lab PC's Windows locale
    a spin box would otherwise show 1.5 as "1,5" (gotcha #18)."""
    loc = QLocale.c()
    loc.setNumberOptions(QLocale.OmitGroupSeparator)
    return loc


def _spin(value=0.0, lo=-1e6, hi=1e6, step=0.1, decimals=4) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setLocale(_c_locale())
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    s.setButtonSymbols(QDoubleSpinBox.NoButtons)
    s.setMinimumWidth(80)
    return s


def _fmt(v, nd=4) -> str:
    return "--" if v is None or v != v else f"{v:.{nd}f}"


# --------------------------------------------------------------------------- #
# signature indicator widget
# --------------------------------------------------------------------------- #
class RailIndicator(QWidget):
    """The rail seen from the side: scale, reference marks, soft limits, the
    carriage and its target.

    * The carriage is solid while the encoder is referenced; before that it is
      drawn as a DASHED outline with a "?" -- the number under it is counted
      from wherever the stage was switched on, not from the rail.
    * While it moves, a sawtooth trails behind it: the stick-slip drive's
      slow "stick" ramps and sudden "slips" (animated on its own ~33 ms timer).
    * The distance-coded reference marks sit on the rail as small ticks, every
      other one slightly shifted -- that shift is the code.
    """

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self._pos = float("nan")
        self._target = float("nan")
        self._lo, self._hi = -115.0, 115.0
        self._moving = False
        self._referenced = False
        self._referencing = False
        self._speed = 0.0
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, pos, target, lo, hi, moving, referenced, referencing, speed) -> None:
        self._pos, self._target = pos, target
        self._lo, self._hi = float(lo), float(hi)
        self._moving, self._referenced = bool(moving), bool(referenced)
        self._referencing, self._speed = bool(referencing), float(speed or 0.0)
        if self._moving:
            if not self._timer.isActive():
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.18) % 1.0
        self.update()

    # -- geometry --------------------------------------------------------- #
    def _span(self) -> tuple[float, float]:
        """Drawn range: the soft limits plus a little rail beyond them."""
        lo, hi = min(self._lo, self._hi), max(self._lo, self._hi)
        pad = max(1.0, 0.04 * (hi - lo))
        return lo - pad, hi + pad

    def _x(self, mm: float, r: QRectF) -> float:
        a, b = self._span()
        f = (mm - a) / (b - a) if b > a else 0.5
        return r.left() + min(1.0, max(0.0, f)) * r.width()

    def paintEvent(self, _event) -> None:
        C = theme.COLORS
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        rail = QRectF(18, h * 0.62, w - 36, 12)

        # background plate
        p.setPen(QPen(QColor(C["border"]), 1))
        p.setBrush(QColor(C["code_bg"]))
        p.drawRoundedRect(QRectF(4, 4, w - 8, h - 8), 8, 8)

        # the rail
        p.setPen(QPen(QColor(C["border"]), 1))
        p.setBrush(QColor(C["panel_hi"]))
        p.drawRoundedRect(rail, 4, 4)

        # scale ticks every 10 mm, labels every 50 mm
        a, b = self._span()
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.8))
        p.setFont(small)
        k0, k1 = int(math.ceil(a / 10.0)), int(math.floor(b / 10.0))
        for k in range(k0, k1 + 1):
            mm = k * 10.0
            x = self._x(mm, rail)
            major = k % 5 == 0
            p.setPen(QPen(QColor(C["muted"] if major else C["grid"]), 1))
            p.drawLine(QPointF(x, rail.bottom() + 3), QPointF(x, rail.bottom() + (10 if major else 5)))
            if major:
                p.setPen(QColor(C["muted"]))
                p.drawText(QRectF(x - 30, rail.bottom() + 11, 60, 14),
                           Qt.AlignHCenter | Qt.AlignTop, f"{mm:g}")

        # distance-coded reference marks on the rail (drawn exaggerated)
        p.setPen(QPen(QColor(C["accent_dim"]), 1.4))
        n0 = int(math.ceil(a / MARK_PITCH_MM))
        n1 = int(math.floor(b / MARK_PITCH_MM))
        for k in range(n0, n1 + 1):
            mm = k * MARK_PITCH_MM + (MARK_CODE_MM if k % 2 else 0.0)
            x = self._x(mm, rail) + (2.5 if k % 2 else 0.0)
            p.drawLine(QPointF(x, rail.top() + 2), QPointF(x, rail.bottom() - 2))

        # soft limits
        p.setPen(QPen(QColor(C["danger"]), 2))
        for lim in (self._lo, self._hi):
            x = self._x(lim, rail)
            p.drawLine(QPointF(x, rail.top() - 18), QPointF(x, rail.bottom() + 2))

        pos_ok = self._pos == self._pos
        cw = max(26.0, min(70.0, rail.width() * 0.09))
        cx = self._x(self._pos, rail) if pos_ok else rail.center().x()
        car = QRectF(cx - cw / 2, rail.top() - 26, cw, 24)

        # target marker (a small downward triangle)
        if self._target == self._target:
            tx = self._x(self._target, rail)
            tri = QPolygonF([QPointF(tx - 6, car.top() - 14), QPointF(tx + 6, car.top() - 14),
                             QPointF(tx, car.top() - 5)])
            p.setPen(QPen(QColor(C["accent_hi"]), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(tri)

        # stick-slip sawtooth trailing the carriage while it moves
        if self._moving:
            direction = 1.0 if self._speed >= 0 else -1.0
            path = QPainterPath()
            y0 = car.center().y()
            tooth, amp, n = 9.0, 6.0, 6
            start = cx - direction * cw / 2
            path.moveTo(start, y0)
            for i in range(n):
                # slow "stick" ramp, sudden "slip" back: the drive's rhythm,
                # scrolling with the phase so it reads as motion
                base = start - direction * (i + self._phase) * tooth
                path.lineTo(base - direction * tooth * 0.85, y0 - amp)
                path.lineTo(base - direction * tooth, y0 + amp * 0.2)
            col = QColor(C["accent"])
            col.setAlpha(170)
            p.setPen(QPen(col, 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

        # the carriage
        if self._referenced:
            fill = QColor(C["accent"] if self._moving else C["accent_dim"])
            p.setPen(QPen(QColor(C["accent_hi"]), 1.5))
            p.setBrush(fill)
        else:
            p.setPen(QPen(QColor(C["accent_hi"] if self._moving else C["muted"]), 1.5, Qt.DashLine))
            p.setBrush(QBrush(QColor(C["panel_hi"])))
        p.drawRoundedRect(car, 4, 4)
        p.setPen(QColor(C["text"] if not self._referenced else "#141414"))
        bold = QFont(self.font())
        bold.setBold(True)
        p.setFont(bold)
        p.drawText(car, Qt.AlignCenter, "?" if not self._referenced else "")

        # the position above the carriage
        p.setFont(small)
        p.setPen(QColor(C["text"]))
        label = f"{self._pos:.3f} mm" if pos_ok else "-- mm"
        p.drawText(QRectF(cx - 60, car.top() - 34, 120, 16), Qt.AlignCenter, label)

        # reference badge, top-left
        if self._referencing:
            txt, col = "SEARCHING REF", C["accent"]
        elif self._referenced:
            txt, col = "REFERENCED", C["ok"]
        else:
            txt, col = "NOT REFERENCED", C["danger"]
        p.setFont(bold)
        fm = p.fontMetrics()
        bw = fm.horizontalAdvance(txt) + 16
        badge = QRectF(12, 12, bw, 20)
        p.setPen(QPen(QColor(col), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(badge, 10, 10)
        p.setPen(QColor(col))
        p.drawText(badge, Qt.AlignCenter, txt)
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
        self.setWindowTitle("SmarAct linear stage" + ("  [remote]" if remote else ""))
        self.resize(1180, 760)

        self._bridge = Bridge()
        self._bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda level, msg: self._bridge.event.emit(level, msg)

        self._build_ui()

        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()

        self._reload_positions()
        try:
            st = self.ctrl.status()
            self._on_event("info", "connected" if st.connected else "not connected")
            if st.connected and not st.referenced:
                self._on_event("warn", "axis NOT referenced: absolute moves are refused "
                                       "until Find reference has run")
        except Exception:
            pass
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
        title = QLabel("SMARACT LINEAR STAGE")
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

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(self._build_readout_card())
        left.addWidget(self._build_move_card())
        left.addWidget(self._build_speed_card())
        left.addWidget(self._build_log_card(), 1)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(self._build_positions_card(), 1)
        body.addLayout(left, 3)
        body.addLayout(right, 2)
        root.addLayout(body, 1)

    def _build_readout_card(self) -> QFrame:
        frame, lay = _card("POSITION")
        row = QHBoxLayout()
        col = QVBoxLayout()
        self._big = QLabel("--")
        self._big.setObjectName("bigValue")
        cap = QLabel("mm, absolute encoder scale")
        cap.setObjectName("caption")
        col.addWidget(self._big)
        col.addWidget(cap)
        row.addLayout(col)
        row.addStretch(1)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self._sub = {}
        for i, (key, label) in enumerate((("target", "target"), ("relative", "relative"),
                                          ("speed", "speed"), ("state", "state"))):
            k = QLabel(label)
            k.setObjectName("muted")
            v = QLabel("--")
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self._sub[key] = v
        row.addLayout(grid)
        lay.addLayout(row)
        self._indicator = RailIndicator()
        lay.addWidget(self._indicator)
        return frame

    def _build_move_card(self) -> QFrame:
        frame, lay = _card("MOVE")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        grid.addWidget(QLabel("go to"), 0, 0)
        self._goto = _spin(0.0, -1e4, 1e4, 0.1, 4)
        grid.addWidget(self._goto, 0, 1)
        grid.addWidget(QLabel("mm"), 0, 2)
        go = QPushButton("Move")
        go.setObjectName("primary")
        repolish(go)
        go.clicked.connect(self._move)
        grid.addWidget(go, 0, 3)
        self._from_zero = QCheckBox("from zero")
        self._from_zero.setToolTip("Target measured from the 'zero here' origin")
        grid.addWidget(self._from_zero, 0, 4)

        grid.addWidget(QLabel("jog"), 1, 0)
        self._jog_step = _spin(self.cfg.motion.jog_step_mm, 0.0, 1000.0, 0.01, 4)
        grid.addWidget(self._jog_step, 1, 1)
        grid.addWidget(QLabel("mm"), 1, 2)
        jr = QHBoxLayout()
        minus = QPushButton("−")
        plus = QPushButton("+")
        for b, s in ((minus, -1), (plus, +1)):
            b.setMinimumWidth(44)
            b.clicked.connect(lambda _c, sign=s: self._jog(sign))
            jr.addWidget(b)
        grid.addLayout(jr, 1, 3, 1, 2)
        lay.addLayout(grid)

        row = QHBoxLayout()
        ref = QPushButton("Find reference")
        ref.setToolTip("Drives over two distance-coded reference marks (a few mm)")
        ref.clicked.connect(lambda: self._do(self.ctrl.find_reference))
        zero = QPushButton("Zero here")
        zero.clicked.connect(lambda: self._do(self.ctrl.set_zero))
        clr = QPushButton("Clear zero")
        clr.clicked.connect(lambda: self._do(self.ctrl.clear_zero))
        stop = QPushButton("STOP")
        stop.setObjectName("danger")
        repolish(stop)
        stop.clicked.connect(lambda: self._do(self.ctrl.stop))
        mark_always(stop)            # the SAFETY verb: a viewer can always stop the carriage
        for b in (ref, zero, clr):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(stop)
        lay.addLayout(row)
        return frame

    def _build_speed_card(self) -> QFrame:
        frame, lay = _card("SPEED  /  HOLD")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.addWidget(QLabel("velocity"), 0, 0)
        self._vel = _spin(self.cfg.motion.velocity_mm_s, 0.0, 1000.0, 0.1, 3)
        grid.addWidget(self._vel, 0, 1)
        grid.addWidget(QLabel("mm/s"), 0, 2)
        setv = QPushButton("Set")
        setv.clicked.connect(lambda: self._do(lambda: self.ctrl.set_velocity(self._vel.value())))
        grid.addWidget(setv, 0, 3)
        self._freq = QLabel("-- Hz")
        self._freq.setObjectName("muted")
        grid.addWidget(self._freq, 0, 4)

        grid.addWidget(QLabel("hold"), 1, 0)
        self._hold = QSpinBox()
        self._hold.setLocale(_c_locale())
        self._hold.setRange(0, 60000)
        self._hold.setSingleStep(100)
        self._hold.setValue(int(self.cfg.motion.hold_time_ms))
        self._hold.setButtonSymbols(QSpinBox.NoButtons)
        grid.addWidget(self._hold, 1, 1)
        grid.addWidget(QLabel("ms"), 1, 2)
        seth = QPushButton("Set")
        seth.clicked.connect(lambda: self._do(lambda: self.ctrl.set_hold_time(self._hold.value())))
        grid.addWidget(seth, 1, 3)
        hint = QLabel("0 = let go at the target")
        hint.setObjectName("hint")
        grid.addWidget(hint, 1, 4)
        grid.setColumnStretch(4, 1)
        lay.addLayout(grid)
        return frame

    def _build_positions_card(self) -> QFrame:
        frame, lay = _card("STORED POSITIONS  (absolute mm)")
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["#", "name", "mm"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        lay.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._slot_name = QLineEdit()
        self._slot_name.setPlaceholderText("name for the selected slot")
        store = QPushButton("Store")
        store.setObjectName("primary")
        repolish(store)
        goto = QPushButton("Go to")
        clear = QPushButton("Clear")
        store.clicked.connect(self._store_current)
        goto.clicked.connect(self._goto_selected)
        clear.clicked.connect(self._clear_selected)
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
            self._on_event("info", "settings applied")

    def _push_config(self) -> None:
        """Apply edited config to the running controller (local or remote)."""
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

    def _move(self) -> None:
        val = self._goto.value()
        if self._from_zero.isChecked():
            self._do(lambda: self.ctrl.move_from_zero(val))
        else:
            self._do(lambda: self.ctrl.move_to(val))

    def _jog(self, sign: int) -> None:
        step = self._jog_step.value()
        self._do(lambda: self.ctrl.move_by(sign * step))

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
            cells = [str(i), p.get("name", "") if used else "—",
                     f"{p.get('position_mm', 0):.4f}" if used else ""]
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
        self._big.setText(_fmt(st.position_mm))
        self._sub["target"].setText(_fmt(st.target_mm) + " mm")
        self._sub["relative"].setText(_fmt(st.relative_mm) + " mm")
        self._sub["speed"].setText(f"{st.speed_mm_s:.3f} mm/s")
        state = st.channel_state + ("" if st.connected else "  (disconnected)")
        if st.hw_error:
            state = "HW ERROR"
        self._sub["state"].setText(state)
        self._freq.setText(f"{st.max_frequency_hz} Hz")
        self._indicator.set_state(st.position_mm, st.target_mm, st.min_mm, st.max_mm,
                                  st.moving, st.referenced, st.referencing, st.speed_mm_s)

    def _on_event(self, level: str, msg: str) -> None:
        color = {"info": theme.COLORS["muted"], "warn": theme.COLORS["accent_hi"],
                 "error": theme.COLORS["danger"]}.get(level, theme.COLORS["text"])
        self._log.appendHtml(f'<span style="color:{color}">[{level}]</span> {msg}')


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run_app(ctrl, cfg: Config, remote: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    # Choose the palette BEFORE any widget is built, so every widget and custom
    # paintEvent reads the right COLORS. Startup-only: there is no live toggle.
    theme.set_theme(getattr(cfg.ui, "theme", "dark"))
    QLocale.setDefault(_c_locale())   # gotcha #18: "1,5" on a comma locale

    app = QApplication.instance() or QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    theme.apply_palette(app)
    app.setStyleSheet(theme.build_stylesheet())

    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """Open the panel on a private SIMULATED positioner (no service, no hardware).

    Also what tools/render_panels.py calls to draw the front-panel picture, so
    it shows the honest start state: connected, NOT referenced, nothing moving.
    """
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
