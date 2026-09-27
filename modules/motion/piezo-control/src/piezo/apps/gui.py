"""The front panel (§7 of the guide): MainWindow + the signature indicator.

PySide6, Fusion style, dark/amber theme.  ``run_app(ctrl, cfg, remote=False)``
builds the window; ``ctrl`` is either a local :class:`Piezo` brain or a
:class:`PiezoClient` -- they share a method surface, so the GUI is agnostic.

Layout: a horizontal splitter.
  * LEFT  = the "visual" side: big X/Y read-outs, loop-mode badges, and the
    signature :class:`PiezoIndicator` (a top-down map of the travel envelope).
  * RIGHT = controls (move/jog/relative, loop toggles, velocity + ramp mode,
    the position list, the log), in a scroll area.

Threading rule: instrument events arrive on a background thread, so they MUST
cross into Qt through a signal -- see :class:`Bridge`.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
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
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Config, RAMP_MODES
from . import theme
from .settings_dialog import SettingsDialog
from .theme import repolish

AXES = ("X", "Y")


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


def _spin(value=0.0, lo=-1e6, hi=1e6, step=0.1, decimals=3) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    s.setButtonSymbols(QDoubleSpinBox.NoButtons)
    s.setMinimumWidth(80)
    return s


# --------------------------------------------------------------------------- #
# signature indicator widget
# --------------------------------------------------------------------------- #
class PiezoIndicator(QWidget):
    """Top-down XY map of the piezo travel envelope.

    * A square plot represents each axis' full travel (0 .. travel_max, which
      shrinks when an axis is in closed loop).
    * A crosshair + dot marks the live measured position; a faint ring marks the
      commanded target while a move is in flight.
    * The marker pulses amber (its own ~33 ms QTimer, independent of the status
      poll) while either axis is moving.
    * Corner badges show each axis' loop mode: a filled GREEN dot = closed loop
      (servo on), an amber RING = open loop.
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setMinimumHeight(300)
        self._pos = [0.0, 0.0]
        self._target = [0.0, 0.0]
        self._moving = [False, False]
        self._closed = [True, True]
        self._tmax = [cfg.limits.travel_max_ol, cfg.limits.travel_max_ol]
        self._phase = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, pos, target, moving, closed, tmax) -> None:
        self._pos = list(pos)
        self._target = list(target)
        self._moving = list(moving)
        self._closed = list(closed)
        self._tmax = list(tmax)
        if any(moving):
            if not self._timer.isActive():
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()
            self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.12) % (2 * math.pi)
        self.update()

    def _frac(self, axis: int, value: float) -> float:
        lo = self.cfg.limits.travel_min
        hi = self._tmax[axis]
        if hi <= lo:
            return 0.5
        if value != value:  # NaN
            return 0.5
        return min(1.0, max(0.0, (value - lo) / (hi - lo)))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        margin = 20
        side = min(w - 2 * margin, h - 2 * margin)
        px = margin + (w - 2 * margin - side) / 2
        py = margin + (h - 2 * margin - side) / 2
        plot = _Rect(px, py, side, side)

        # envelope
        p.setPen(QPen(QColor(theme.COLORS["border"]), 1))
        p.setBrush(QColor(theme.COLORS["code_bg"]))
        p.drawRoundedRect(plot.x, plot.y, plot.w, plot.h, 8, 8)

        # grid
        p.setPen(QPen(QColor(theme.COLORS["grid"]), 1))
        for i in range(1, 4):
            gx = plot.x + plot.w * i / 4
            gy = plot.y + plot.h * i / 4
            p.drawLine(int(gx), plot.y, int(gx), plot.y + plot.h)
            p.drawLine(plot.x, int(gy), plot.x + plot.w, int(gy))

        # marker position (Y grows upward)
        fx = self._frac(0, self._pos[0])
        fy = self._frac(1, self._pos[1])
        mx = plot.x + fx * plot.w
        my = plot.y + (1.0 - fy) * plot.h

        moving = any(self._moving)

        # target ghost + tether while moving
        if moving:
            tx = plot.x + self._frac(0, self._target[0]) * plot.w
            ty = plot.y + (1.0 - self._frac(1, self._target[1])) * plot.h
            p.setPen(QPen(QColor(theme.COLORS["accent_dim"]), 1, Qt.DashLine))
            p.drawLine(int(mx), int(my), int(tx), int(ty))
            ring = QColor(theme.COLORS["accent_hi"])
            ring.setAlpha(160)
            p.setPen(QPen(ring, 1))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(int(tx - 5), int(ty - 5), 10, 10)

        # crosshair through the marker
        p.setPen(QPen(QColor(theme.COLORS["muted"]), 1, Qt.DashLine))
        p.drawLine(plot.x, int(my), plot.x + plot.w, int(my))
        p.drawLine(int(mx), plot.y, int(mx), plot.y + plot.h)

        # pulsing halo while moving
        if moving:
            r = 10 + 6 * (0.5 + 0.5 * math.sin(self._phase))
            halo = QColor(theme.COLORS["accent"])
            halo.setAlpha(70)
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawEllipse(int(mx - r), int(my - r), int(2 * r), int(2 * r))

        # marker dot
        base = QColor(theme.COLORS["accent"]) if moving else QColor(theme.COLORS["accent_dim"])
        p.setPen(QPen(QColor(theme.COLORS["accent_hi"]), 2))
        p.setBrush(base)
        p.drawEllipse(int(mx - 6), int(my - 6), 12, 12)

        # axis labels
        p.setPen(QColor(theme.COLORS["muted"]))
        p.drawText(plot.x, plot.y + plot.h + 15, "X →")
        p.save()
        p.translate(plot.x - 6, plot.y + plot.h)
        p.rotate(-90)
        p.drawText(0, 0, "Y →")
        p.restore()

        # loop-mode badges (top-left corner)
        for a in range(2):
            cx = plot.x + 12 + a * 54
            cy = plot.y + 14
            if self._closed[a]:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(theme.COLORS["ok"]))
                p.drawEllipse(cx - 5, cy - 5, 10, 10)
                label = f"{AXES[a]} CL"
                col = theme.COLORS["ok"]
            else:
                p.setPen(QPen(QColor(theme.COLORS["accent"]), 2))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(cx - 5, cy - 5, 10, 10)
                label = f"{AXES[a]} OL"
                col = theme.COLORS["accent"]
            p.setPen(QColor(col))
            p.drawText(cx + 9, cy + 4, label)
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
        self.setWindowTitle("2D Piezo Stage" + ("  [remote]" if remote else ""))
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
        self._sync_controls_from_status()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # top bar
        top = QHBoxLayout()
        title = QLabel("2D PIEZO STAGE  ·  JENA d-DRIVE / PXY-200")
        title.setObjectName("cardTitle")
        top.addWidget(title)
        top.addStretch(1)
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        top.addWidget(settings_btn)
        root.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)

        # LEFT: visual side
        left = QWidget()
        lcol = QVBoxLayout(left)
        lcol.setContentsMargins(0, 0, 0, 0)
        lcol.setSpacing(12)
        lcol.addWidget(self._build_readout_card())
        lcol.addWidget(self._build_indicator_card(), 1)

        # RIGHT: controls
        controls = QWidget()
        col = QVBoxLayout(controls)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        col.addWidget(self._build_move_card())
        col.addWidget(self._build_loop_velocity_card())
        col.addWidget(self._build_positions_card(), 1)
        col.addWidget(self._build_log_card(), 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(controls)
        scroll.setMinimumWidth(440)

        splitter.addWidget(left)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([620, 500])
        root.addWidget(splitter, 1)

    def _build_readout_card(self) -> QFrame:
        frame, lay = _card("POSITION  (measured · target · rel)")
        row = QHBoxLayout()
        self._big = []
        self._sub_lbl = []
        for a in range(2):
            colw = QVBoxLayout()
            cap = QLabel(AXES[a] + "  (um)")
            cap.setObjectName("caption")
            big = QLabel("--")
            big.setObjectName("bigValue")
            sub = QLabel("tgt -- · rel --")
            sub.setObjectName("muted")
            colw.addWidget(cap)
            colw.addWidget(big)
            colw.addWidget(sub)
            row.addLayout(colw)
            self._big.append(big)
            self._sub_lbl.append(sub)
        lay.addLayout(row)
        return frame

    def _build_indicator_card(self) -> QFrame:
        frame, lay = _card("TRAVEL MAP")
        self._indicator = PiezoIndicator(self.cfg)
        lay.addWidget(self._indicator, 1)
        return frame

    def _build_move_card(self) -> QFrame:
        frame, lay = _card("MOVE  /  JOG")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self._target = []
        for a in range(2):
            grid.addWidget(QLabel(AXES[a]), a, 0)
            sp = _spin(0.0, -1e6, 1e6, 1.0)
            grid.addWidget(sp, a, 1)
            self._target.append(sp)

            move_btn = QPushButton("Move")
            move_btn.setMaximumWidth(64)
            move_btn.clicked.connect(lambda _c, ax=a: self._move(ax))
            grid.addWidget(move_btn, a, 2)

            minus = QPushButton("−")
            plus = QPushButton("+")
            minus.setMaximumWidth(32)
            plus.setMaximumWidth(32)
            minus.clicked.connect(lambda _c, ax=a: self._jog(ax, -1))
            plus.clicked.connect(lambda _c, ax=a: self._jog(ax, +1))
            grid.addWidget(minus, a, 3)
            grid.addWidget(plus, a, 4)

            zero = QPushButton("Zero")
            zero.setMaximumWidth(54)
            zero.setToolTip("Set this axis' current position as its relative zero")
            zero.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.set_zero(ax)))
            grid.addWidget(zero, a, 5)
        lay.addLayout(grid)

        mrow = QHBoxLayout()
        from PySide6.QtWidgets import QCheckBox
        self._rel_mode = QCheckBox("Relative mode  (targets measured from the zero)")
        mrow.addWidget(self._rel_mode)
        mrow.addStretch(1)
        lay.addLayout(mrow)

        row = QHBoxLayout()
        row.addWidget(QLabel("jog step (um)"))
        self._jog_step = _spin(self.cfg.motion.jog_step, 0.0, 1000.0, 0.5, 3)
        row.addWidget(self._jog_step)
        row.addStretch(1)
        xy_btn = QPushButton("Move XY")
        xy_btn.setObjectName("primary")
        repolish(xy_btn)
        xy_btn.clicked.connect(
            lambda: self._do(lambda: self.ctrl.move_xy(self._target[0].value(), self._target[1].value()))
        )
        row.addWidget(xy_btn)
        zero_all = QPushButton("Zero all")
        zero_all.clicked.connect(lambda: self._do(self.ctrl.set_zero_all))
        row.addWidget(zero_all)
        stop = QPushButton("STOP")
        stop.setObjectName("danger")
        repolish(stop)
        stop.clicked.connect(lambda: self._do(self.ctrl.stop_all))
        row.addWidget(stop)
        lay.addLayout(row)
        return frame

    def _build_loop_velocity_card(self) -> QFrame:
        frame, lay = _card("LOOP MODE  /  VELOCITY")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.addWidget(QLabel(""), 0, 0)
        grid.addWidget(QLabel("closed loop"), 0, 1)
        grid.addWidget(QLabel("velocity (um/s)"), 0, 2, 1, 2)

        self._loop_btn = []
        self._vel = []
        for a in range(2):
            grid.addWidget(QLabel(AXES[a]), a + 1, 0)
            btn = QPushButton("closed")
            btn.setCheckable(True)
            btn.setToolTip("Toggle closed-loop (servo, ~160 um) vs open-loop (~200 um)")
            btn.clicked.connect(lambda _c, ax=a: self._toggle_loop(ax))
            self._loop_btn.append(btn)
            grid.addWidget(btn, a + 1, 1)

            sp = _spin(self.cfg.motion.vel_x if a == 0 else self.cfg.motion.vel_y, 0.0, 1e6, 10.0, 2)
            self._vel.append(sp)
            grid.addWidget(sp, a + 1, 2)
            setb = QPushButton("Set")
            setb.setMaximumWidth(52)
            setb.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.set_velocity(ax, self._vel[ax].value())))
            grid.addWidget(setb, a + 1, 3)
        lay.addLayout(grid)

        row = QHBoxLayout()
        row.addWidget(QLabel("ramp mode"))
        self._ramp_combo = QComboBox()
        self._ramp_combo.addItems(list(RAMP_MODES))
        self._ramp_combo.setToolTip(
            "hardware = controller slew-rate limiter · software = timed setpoint ramp · off = fastest"
        )
        self._ramp_combo.activated.connect(
            lambda _i: self._do(lambda: self.ctrl.set_ramp_mode(self._ramp_combo.currentText()))
        )
        row.addWidget(self._ramp_combo)
        row.addStretch(1)
        lay.addLayout(row)
        return frame

    def _build_positions_card(self) -> QFrame:
        frame, lay = _card("POSITION LIST  (20 slots, um)")
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["#", "name", "X", "Y"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
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
            self._sync_controls_from_status()
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

    def _move(self, axis: int) -> None:
        val = self._target[axis].value()
        if self._rel_mode.isChecked():
            self._do(lambda: self.ctrl.move_relative(axis, val))
        else:
            self._do(lambda: self.ctrl.move_axis(axis, val))

    def _jog(self, axis: int, sign: int) -> None:
        step = self._jog_step.value()
        current = self.ctrl.status().position[axis]
        if current != current:
            current = 0.0
        self._do(lambda: self.ctrl.move_axis(axis, current + sign * step))

    def _toggle_loop(self, axis: int) -> None:
        want = self._loop_btn[axis].isChecked()
        self._do(lambda: self.ctrl.set_closed_loop(axis, want))

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
                f"{p.get('x', 0):.3f}" if used else "",
                f"{p.get('y', 0):.3f}" if used else "",
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
        for a in range(2):
            val = st.position[a]
            self._big[a].setText("--" if val != val else f"{val:.3f}")
            tgt = st.target[a]
            rel = st.relative[a]
            tgt_txt = "tgt --" if tgt != tgt else f"tgt {tgt:.3f}"
            rel_txt = "rel --" if rel != rel else f"rel {rel:.3f}"
            self._sub_lbl[a].setText(f"{tgt_txt} · {rel_txt}")
            # keep the loop button label/checkstate honest without stealing focus
            btn = self._loop_btn[a]
            if btn.isChecked() != bool(st.closed_loop[a]):
                btn.blockSignals(True)
                btn.setChecked(bool(st.closed_loop[a]))
                btn.blockSignals(False)
            btn.setText("closed" if st.closed_loop[a] else "open")
        self._indicator.set_state(st.position, st.target, st.moving, st.closed_loop, st.travel_max)

    def _sync_controls_from_status(self) -> None:
        """Set the loop buttons / ramp combo from the live status once at start."""
        st = self.ctrl.status()
        for a in range(2):
            self._loop_btn[a].blockSignals(True)
            self._loop_btn[a].setChecked(bool(st.closed_loop[a]))
            self._loop_btn[a].setText("closed" if st.closed_loop[a] else "open")
            self._loop_btn[a].blockSignals(False)
        idx = self._ramp_combo.findText(getattr(st, "ramp_mode", self.cfg.motion.ramp_mode))
        if idx >= 0:
            self._ramp_combo.setCurrentIndex(idx)

    def _on_event(self, level: str, msg: str) -> None:
        color = {"info": theme.COLORS["muted"], "warn": theme.COLORS["accent_hi"], "error": theme.COLORS["danger"]}.get(level, theme.COLORS["text"])
        self._log.appendHtml(f'<span style="color:{color}">[{level}]</span> {msg}')


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run_app(ctrl, cfg: Config, remote: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from .theme import apply_window_icon
    apply_window_icon(app)
    # Theme is a startup setting: pick the palette BEFORE any widget is built,
    # so custom-painted widgets (the travel map) read the right COLORS.
    theme.set_theme(getattr(cfg.ui, "theme", "dark"))
    app.setStyle("Fusion")
    theme.apply_palette(app)
    app.setStyleSheet(theme.build_stylesheet())

    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()
