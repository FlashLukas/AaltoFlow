"""Control GUI for the scalar network analyser (Signal Hound TG via the
signalhound service, or the simulator).

    uv run scripts/run_gui.py                  # local simulator
    uv run scripts/run_gui.py --real           # TG sweeps via the signalhound service
    uv run scripts/run_gui.py --connect HOST   # a running shsna service (either kind)

The window holds an Analyzer-like object (an in-process Analyzer or a
ShsnaClient facade) and never cares which. A 60 ms timer reads status(); a
trace is fetched only when a new one exists. Events cross into the GUI thread
on a Qt signal.

Signature widget: ChainIndicator -- the measurement chain TG -> DUT ->
analyser, with a pulse running along it while a TG sweep is in flight, the
analyser drawn red when the owner service is unreachable or reports an error,
and the DUT drawn as a plain thru when the simulator has it removed.

The plots, both against the BRAIN's reference (so the GUI, the console and a
scan all mean the same reference):
  top     transmission |S21| in dB = measured - thru reference
  bottom  what the analyser measured and the reference itself, both in dB
          relative to the TG output (the TG44A's unit), so a bad reference (a
          cable left out) is seen at a glance
"""

from __future__ import annotations

import math
import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from .. import physics
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog


class Bridge(QtCore.QObject):
    """Carries analyser events across the thread boundary into the GUI."""
    event = QtCore.Signal(str, str)


def _card(title: str | None = None):
    frame = QtWidgets.QFrame()
    frame.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


def _fmt(v, fmt: str, none: str = "--") -> str:
    return format(v, fmt) if isinstance(v, (int, float)) and math.isfinite(v) else none


def _age(seconds) -> str:
    if not (isinstance(seconds, (int, float)) and math.isfinite(seconds)):
        return "--"
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


# ------------------------------------------------------------- the indicator

class ChainIndicator(QtWidgets.QWidget):
    """TG -> DUT -> analyser, with the sweep running along it."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 120)
        self._sweeping = False
        self._ok = True
        self._dut = True
        self._reference = False
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, sweeping: bool, ok: bool, dut_inserted: bool, taking_reference: bool):
        self._sweeping, self._ok = bool(sweeping), bool(ok)
        self._dut, self._reference = bool(dut_inserted), bool(taking_reference)

    def _tick(self):
        if not self.isVisible():
            return
        if self._sweeping:
            self._phase = (self._phase + 0.02) % 1.0
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        bw, bh = 62.0, 40.0
        y = h / 2 - 8
        xs = [18.0, (w - bw) / 2, w - bw - 18.0]
        labels = ["TG", "THRU" if not self._dut else "DUT", "SA"]
        accent = QtGui.QColor(COLORS["accent"])
        muted = QtGui.QColor(COLORS["muted"])
        danger = QtGui.QColor(COLORS["danger"])

        # the cable, and the tone travelling along it while a sweep runs
        y_mid = y + bh / 2
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["border"]), 3))
        p.drawLine(QtCore.QPointF(xs[0] + bw, y_mid), QtCore.QPointF(xs[2], y_mid))
        if self._sweeping:
            x0, x1 = xs[0] + bw, xs[2]
            x = x0 + (x1 - x0) * self._phase
            glow = QtGui.QRadialGradient(x, y_mid, 14)
            g0 = QtGui.QColor(accent); g0.setAlpha(200)
            g1 = QtGui.QColor(accent); g1.setAlpha(0)
            glow.setColorAt(0, g0); glow.setColorAt(1, g1)
            p.setPen(QtCore.Qt.NoPen); p.setBrush(glow)
            p.drawEllipse(QtCore.QPointF(x, y_mid), 14, 14)

        f = p.font(); f.setBold(True); f.setPointSize(9); p.setFont(f)
        for i, (x, text) in enumerate(zip(xs, labels)):
            rect = QtCore.QRectF(x, y, bw, bh)
            if i == 2 and not self._ok:
                edge = danger
            elif i == 1 and not self._dut:
                edge = muted
            else:
                edge = accent if self._sweeping else muted
            pen = QtGui.QPen(edge, 2)
            if i == 1 and not self._dut:
                pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QtGui.QColor(COLORS["panel_hi"]))
            p.drawRoundedRect(rect, 6, 6)
            p.setPen(QtGui.QColor(COLORS["text"]))
            p.drawText(rect, QtCore.Qt.AlignCenter, text)

        f.setBold(False); f.setPointSize(8); p.setFont(f)
        p.setPen(muted)
        if not self._ok:
            cap, col = "analyser not available", danger
        elif self._reference and self._sweeping:
            cap, col = "taking the thru reference", accent
        elif self._sweeping:
            cap, col = "TG sweep running", accent
        else:
            cap, col = "idle", muted
        p.setPen(col)
        p.drawText(QtCore.QRectF(0, y + bh + 8, w, 16), QtCore.Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._simulated = None           # learnt from the first status
        self.setWindowTitle("Scalar network analyser" + ("  (remote)" if remote else ""))
        self.resize(1240, 800)

        self._raw = None                 # the trace dicts on screen
        self._tx = None
        self._tx_error = ""              # why transmission could not be shown
        self._ref = None
        self._trace_id = -1
        self._last_fetch = 0.0
        self._last_acq = None
        self._ref_id = None

        root = QtWidgets.QWidget(); root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)

        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        try:
            self.ctrl.start()
        except Exception as exc:          # show it, don't crash
            self._on_event("error", f"start failed: {exc}")
        self._sync_inputs(force=True)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget(); panel.setFixedWidth(340)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("SNA")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title)
        self.kind_label = QtWidgets.QLabel(""); self.kind_label.setObjectName("hint")
        header.addWidget(self.kind_label); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        header.addWidget(settings_btn)
        col.addLayout(header)

        ccard, clay = _card("Analyser")
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        clay.addWidget(self.conn_dot)
        self.owner_label = QtWidgets.QLabel("--"); self.owner_label.setObjectName("hint")
        self.owner_label.setWordWrap(True)
        clay.addWidget(self.owner_label)
        self.error_label = QtWidgets.QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        clay.addWidget(self.error_label)
        col.addWidget(ccard)

        # sweep
        scard, slay = _card("TG sweep")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        lim = self.cfg.limits
        self.start_spin = self._dspin(lim.freq_min_Hz / 1e6, lim.freq_max_Hz / 1e6, 3, "  MHz", 10.0)
        self.stop_spin = self._dspin(lim.freq_min_Hz / 1e6, lim.freq_max_Hz / 1e6, 3, "  MHz", 10.0)
        self.points_spin = QtWidgets.QSpinBox(); self.points_spin.setRange(lim.points_min, lim.points_max)
        self.rbw_spin = self._dspin(0.0, lim.rbw_max_Hz / 1e3, 3, "  kHz", 1.0)
        self.rbw_spin.setSpecialValueText("auto")     # 0 = the analyser's default
        self.avg_spin = QtWidgets.QSpinBox(); self.avg_spin.setRange(lim.averages_min, lim.averages_max)
        for label, w in (("Start", self.start_spin), ("Stop", self.stop_spin),
                         ("Points", self.points_spin), ("RBW", self.rbw_spin),
                         ("Averages", self.avg_spin)):
            form.addRow(label, w)
        slay.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        self.sweep_time_label = QtWidgets.QLabel("—"); self.sweep_time_label.setObjectName("hint")
        row.addWidget(self.sweep_time_label, 1)
        apply = QtWidgets.QPushButton("Apply"); apply.setObjectName("primary")
        apply.clicked.connect(self._apply_sweep)
        row.addWidget(apply)
        slay.addLayout(row)
        col.addWidget(scard)

        # acquisition
        acard, alay = _card("Acquire (scan-safe)")
        self.cont_chk = QtWidgets.QCheckBox("Continuous sweep")
        self.cont_chk.setToolTip("Each TG sweep pauses the analyser's spectrum display "
                                 "and any signal-generator output.")
        self.cont_chk.clicked.connect(lambda on: self._call(self.ctrl.set_continuous, on))  # user only
        alay.addWidget(self.cont_chk)
        row = QtWidgets.QHBoxLayout()
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setMinimumHeight(34)
        self.acq_btn.clicked.connect(lambda: self._call(self.ctrl.acquire))
        self.abort_btn = QtWidgets.QPushButton("Abort")
        self.abort_btn.clicked.connect(lambda: self._call(self.ctrl.abort))
        row.addWidget(self.acq_btn, 1); row.addWidget(self.abort_btn)
        alay.addLayout(row)
        self.acq_bar = QtWidgets.QProgressBar(); self.acq_bar.setRange(0, 100)
        self.acq_bar.setTextVisible(False); self.acq_bar.setFixedHeight(6)
        alay.addWidget(self.acq_bar)
        self.sample_label = QtWidgets.QLabel("no acquisition yet"); self.sample_label.setObjectName("hint")
        self.sample_label.setWordWrap(True)
        alay.addWidget(self.sample_label)
        col.addWidget(acard)

        # reference
        rcard, rlay = _card("Thru reference")
        row = QtWidgets.QHBoxLayout()
        self.ref_btn = QtWidgets.QPushButton("Take reference")
        self.ref_btn.setToolTip("Replace the DUT by a thru, then acquire and keep the result "
                                "as the reference that transmission is measured against.")
        self.ref_btn.clicked.connect(lambda: self._call(self.ctrl.take_reference))
        self.clear_ref_btn = QtWidgets.QPushButton("Clear")
        self.clear_ref_btn.clicked.connect(lambda: self._call(self.ctrl.clear_reference))
        row.addWidget(self.ref_btn, 1); row.addWidget(self.clear_ref_btn)
        rlay.addLayout(row)
        self.ref_label = QtWidgets.QLabel("none"); self.ref_label.setObjectName("hint")
        self.ref_label.setWordWrap(True)
        rlay.addWidget(self.ref_label)
        col.addWidget(rcard)

        # the simulated chain (hidden on the real analyser)
        self.sim_card, sim_lay = _card("Simulation")
        self.dut_chk = QtWidgets.QCheckBox("DUT inserted (off = thru)")
        self.dut_chk.clicked.connect(lambda on: self._call(self.ctrl.set_sim, "dut_inserted", on))
        sim_lay.addWidget(self.dut_chk)
        col.addWidget(self.sim_card)
        col.addStretch(1)
        return panel

    @staticmethod
    def _dspin(lo, hi, decimals, suffix, step):
        s = QtWidgets.QDoubleSpinBox()
        s.setDecimals(decimals); s.setRange(lo, hi); s.setSuffix(suffix); s.setSingleStep(step)
        return s

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        rcard, rlay = _card("Transmission")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(20)
        self.big = {}
        for key, label, unit in (("peak", "PEAK |S21|", "dB"), ("freq", "AT", "MHz"),
                                 ("mean", "MEAN", "dB"), ("bw3", "-3 dB WIDTH", "MHz")):
            box = QtWidgets.QVBoxLayout(); box.setSpacing(0)
            box.addStretch(1)
            cap = QtWidgets.QLabel(label); cap.setObjectName("hint")
            box.addWidget(cap)
            line = QtWidgets.QHBoxLayout(); line.setSpacing(6)
            v = QtWidgets.QLabel("—"); v.setObjectName("bigValue")
            # wide enough for "4400.00" (MHz) at the big font: a clipped
            # number is worse than a gap
            v.setMinimumWidth(150 if key in ("freq", "bw3") else 110); v.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            u = QtWidgets.QLabel(unit); u.setObjectName("unit")
            line.addWidget(v); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
            box.addLayout(line)
            box.addStretch(1)
            self.big[key] = v
            row.addLayout(box)
        row.addStretch(1)
        self.indicator = ChainIndicator(); self.indicator.setFixedWidth(320)
        row.addWidget(self.indicator)
        rlay.addLayout(row)
        colw.addWidget(rcard)

        tcard, tlay = _card("Traces")
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Show"))
        self.which_combo = QtWidgets.QComboBox()
        self.which_combo.addItems(["Latest sweep", "Last acquisition"])
        self.which_combo.currentIndexChanged.connect(lambda _i: self._force_fetch())
        bar.addWidget(self.which_combo)
        bar.addStretch(1)
        self.trace_label = QtWidgets.QLabel(""); self.trace_label.setObjectName("hint")
        bar.addWidget(self.trace_label)
        tlay.addLayout(bar)
        import pyqtgraph as pg
        self.tx_plot, self.tx_curve = self._make_plot("|S21|", "dB")
        self.pw_plot, self.raw_curve = self._make_plot("rel. TG output", "dB")
        self.ref_curve = self.pw_plot.plot([], [], pen=pg.mkPen(COLORS["muted"], width=1.2,
                                                                  style=QtCore.Qt.DashLine))
        self.pw_plot.setXLink(self.tx_plot)
        self.peak_line = pg.InfiniteLine(angle=90, movable=False,
                                         pen=pg.mkPen(COLORS["accent_hi"], width=1))
        self.tx_plot.addItem(self.peak_line)
        tlay.addWidget(self.tx_plot, 3)
        tlay.addWidget(self.pw_plot, 2)
        colw.addWidget(tcard, 1)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMaximumHeight(120)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 0)
        return panel

    def _make_plot(self, name, unit):
        import pyqtgraph as pg
        pg.setConfigOptions(antialias=True)
        w = pg.PlotWidget(background=COLORS["code_bg"])
        w.setMinimumHeight(120)
        pen = pg.mkPen(COLORS["muted"])
        for axis in ("left", "bottom"):
            ax = w.getAxis(axis); ax.setPen(pen); ax.setTextPen(pen)
            ax.enableAutoSIPrefix(False)
        w.setLabel("left", name, units=unit)
        w.setLabel("bottom", "frequency", units="MHz")
        w.showGrid(x=True, y=True, alpha=0.15)
        curve = w.plot([], [], pen=pg.mkPen(COLORS["accent"], width=1.6))
        return w, curve

    # ---- actions ---------------------------------------------------------

    def _call(self, fn, *args):
        try:
            r = fn(*args)
            if isinstance(r, dict) and r.get("ok") is False:
                self._on_event("warn", r.get("error", "refused"))
        except Exception as exc:
            self._on_event("warn", f"refused: {exc}")

    def _apply_sweep(self):
        """Send only what changed: every sweep change restarts an acquisition
        and logs an event, and five of them for one click would bury the log."""
        s = self.ctrl.status()
        for spin, now, scale, setter in (
                (self.stop_spin, s.stop_Hz, 1e6, self.ctrl.set_stop),
                (self.start_spin, s.start_Hz, 1e6, self.ctrl.set_start),
                (self.points_spin, s.points, 1, self.ctrl.set_points),
                (self.rbw_spin, s.rbw_Hz, 1e3, self.ctrl.set_rbw),
                (self.avg_spin, s.averages, 1, self.ctrl.set_averages)):
            want = spin.value() * scale
            if not (isinstance(now, (int, float)) and math.isclose(want, now, rel_tol=1e-9, abs_tol=1e-9)):
                self._call(setter, want)
        # a new start may have been clamped until the new stop was in; send it again
        if not math.isclose(self.start_spin.value() * 1e6, self.ctrl.status().start_Hz, abs_tol=1.0):
            self._call(self.ctrl.set_start, self.start_spin.value() * 1e6)

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        SettingsDialog(self.ctrl, self.cfg, lambda: self._sync_inputs(force=True), self).exec()

    # ---- refresh & events ------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    def _sync_inputs(self, force: bool = False):
        """Input boxes follow settings changed ELSEWHERE (console, scan), but
        never while the user is typing in them."""
        s = self.ctrl.status()
        for spin, val in ((self.start_spin, s.start_Hz / 1e6), (self.stop_spin, s.stop_Hz / 1e6),
                          (self.rbw_spin, s.rbw_Hz / 1e3)):
            if isinstance(val, (int, float)) and math.isfinite(val) and (force or not spin.hasFocus()):
                spin.setValue(val)
        for spin, val in ((self.avg_spin, s.averages), (self.points_spin, s.points)):
            if (force or not spin.hasFocus()) and isinstance(val, (int, float)) and val > 0:
                spin.setValue(int(val))
        for chk, val in ((self.cont_chk, s.continuous),
                         (self.dut_chk, getattr(s, "sim_dut_inserted", False))):
            chk.blockSignals(True)
            chk.setChecked(bool(val))
            chk.blockSignals(False)

    def _force_fetch(self):
        self._trace_id = -1
        self._last_fetch = 0.0

    def _set_simulated(self, simulated: bool):
        """Things that depend on WHICH analyser this is, set once it is known."""
        if simulated == self._simulated:
            return
        self._simulated = simulated
        self.kind_label.setText("SIMULATED" if simulated else "via signalhound")
        self.setWindowTitle(("Scalar network analyser - simulated" if simulated
                             else "Scalar network analyser - Signal Hound TG44A")
                            + ("  (remote)" if self._remote else ""))
        self.sim_card.setVisible(simulated)

    def _refresh(self):
        s = self.ctrl.status()
        now = time.monotonic()
        self._set_simulated(bool(getattr(s, "simulated", True)))
        owner = getattr(s, "owner", {}) or {}

        # connection: the owner of the analyser, and anything wrong with it
        if s.hw_error:
            self.conn_dot.setText("●  error")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        elif s.connected:
            self.conn_dot.setText("●  sweeping" if s.sweeping else "●  ready")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        self.conn_dot.setToolTip(s.idn)
        self.owner_label.setText(
            f"{owner.get('address') or '--'}: "
            f"{'reachable' if owner.get('reachable') else 'NOT reachable'}, TG "
            f"{'attached' if owner.get('tg_attached') else 'not attached'}"
            + (f", TG mode {owner.get('tg_mode')}" if owner.get("tg_mode") else ""))
        self.error_label.setText(s.hw_error or "")
        self.error_label.setVisible(bool(s.hw_error))
        self.sweep_time_label.setText(f"about {_fmt(s.sweep_time_s, '.3g')} s per acquisition")
        self.indicator.set_state(s.sweeping, not s.hw_error,
                                 getattr(s, "sim_dut_inserted", True) if self._simulated else True,
                                 getattr(s, "acq_is_reference", False))
        self._sync_inputs()

        # acquisition
        self.acq_bar.setValue(int(100 * s.acq_progress) if s.acquiring else 0)
        self.acq_btn.setEnabled(bool(s.connected) and not s.acquiring)
        self.ref_btn.setEnabled(bool(s.connected) and not s.acquiring)
        self.abort_btn.setEnabled(bool(s.acquiring))
        smp = s.sample or {}
        key = (smp.get("acq_id"), smp.get("failed"))
        if smp and key != self._last_acq:
            self._last_acq = key
            what = "reference" if smp.get("is_reference") else "acquisition"
            if smp.get("failed"):
                self.sample_label.setText(f"#{smp.get('acq_id')}: {what} FAILED: {smp.get('error')}")
                self.sample_label.setStyleSheet(f"color:{COLORS['danger']};")
            else:
                self.sample_label.setStyleSheet("")
                self.sample_label.setText(
                    f"#{smp.get('acq_id')} {what}: {smp.get('points')} points, raw peak "
                    f"{_fmt(smp.get('peak_db'), '.2f')} dB"
                    + (f", |S21| peak {_fmt(smp.get('peak_transmission_db'), '.2f')} dB"
                       if math.isfinite(_num(smp.get("peak_transmission_db"))) else "")
                    + (" OVERLOAD" if smp.get("overload") else ""))
            if self.which_combo.currentIndex() == 1:
                self._force_fetch()

        # reference
        ref = s.reference or {}
        if s.acquiring and getattr(s, "acq_is_reference", False):
            self.ref_label.setText(f"taking reference #{s.acq_id} ...")
        elif ref.get("present"):
            self.ref_label.setText(
                f"#{ref.get('acq_id')}: {_fmt(ref.get('start_Hz', math.nan) / 1e6, '.6g')}-"
                f"{_fmt(ref.get('stop_Hz', math.nan) / 1e6, '.6g')} MHz, {ref.get('points')} pts, "
                f"{_age(ref.get('age_s'))}")
        else:
            self.ref_label.setText("none")
        ref_id = ref.get("acq_id") if ref.get("present") else None
        if ref_id != self._ref_id:
            self._ref_id = ref_id
            self._ref = None
            if ref_id is not None:
                try:
                    self._ref = self.ctrl.get_trace("reference")
                except Exception:
                    self._ref = None
            self._force_fetch()

        # traces: fetch only when there is a new one, and not more than ~7 per second
        source = "last" if self.which_combo.currentIndex() == 0 else "sample"
        new = s.trace_id != self._trace_id if source == "last" else self._trace_id == -1
        if new and now - self._last_fetch > 0.14:
            self._last_fetch = now
            try:
                self._raw = self.ctrl.get_trace("raw", source)
            except Exception as exc:
                self._raw, self._tx = None, None
                self._tx_error = str(exc)
            else:
                try:
                    self._tx = self.ctrl.get_trace("transmission", source)
                    self._tx_error = ""
                except Exception as exc:
                    self._tx = None
                    self._tx_error = str(exc)
            self._trace_id = s.trace_id
            self._redraw()

    def _redraw(self):
        raw, tx, ref = self._raw, self._tx, self._ref
        if raw is not None:
            self.raw_curve.setData(raw["freqs_Hz"] / 1e6, raw["raw"])
            label = (f"{raw['points']} points, "
                     f"RBW {'auto' if not raw.get('rbw_Hz') else format(raw['rbw_Hz'] / 1e3, 'g') + ' kHz'}")
        else:
            self.raw_curve.setData([], [])
            label = self._tx_error or "no trace yet"
        if ref is not None:
            self.ref_curve.setData(ref["freqs_Hz"] / 1e6, ref["reference"])
        else:
            self.ref_curve.setData([], [])
        if tx is not None:
            f = tx["freqs_Hz"]
            t = tx["transmission"]
            self.tx_curve.setData(f / 1e6, t)
            r = physics.summarise_transmission(f, t)
            label += f"  -  against reference #{tx.get('reference_acq_id')}"
            self.big["peak"].setText(_fmt(r["peak_transmission_db"], ".2f"))
            self.big["freq"].setText(_fmt(r["peak_freq_hz"] / 1e6, ".2f"))
            self.big["mean"].setText(_fmt(r["mean_transmission_db"], ".2f"))
            self.big["bw3"].setText(_fmt(r["bw3_hz"] / 1e6, ".2f"))
            pk = r["peak_freq_hz"]
            self.peak_line.setVisible(math.isfinite(pk))
            if math.isfinite(pk):
                self.peak_line.setPos(pk / 1e6)
        else:
            self.tx_curve.setData([], [])
            self.peak_line.setVisible(False)
            for v in self.big.values():
                v.setText("—")
            if raw is not None:
                label += "  -  no transmission: " + (self._tx_error or "no reference")
        self.trace_label.setText(label)
        self.trace_label.setToolTip(label)

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) else math.nan


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with an Analyzer-like object. The theme is chosen ONCE
    here, from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    # '.' decimal point and no thousands separator whatever the Windows locale
    # (suite gotcha #18).
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    QtCore.QLocale.setDefault(loc)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())
    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """Run against the built-in simulator, in-process.

    Continuous sweeping is switched ON here, and a thru reference is taken
    before the DUT goes in: there is no real analyser to disturb, and an empty
    screen explains nothing. (The service keeps the rule: nothing sweeps at
    start.)"""
    from ..config import Config
    from ..sim_system import build_sim_system
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz = 700e6, 1300e6
    cfg.acquisition.continuous = True
    cfg.sim.dut_inserted = False
    sna, _ = build_sim_system(cfg)
    _demo_reference_then_dut(sna)
    return run_app(sna, cfg)


def _demo_reference_then_dut(sna) -> None:
    """For the standalone simulator: once the thread runs, take the thru
    reference, then insert the DUT -- what an operator does first."""
    import threading

    def work():
        for _ in range(100):                      # wait for start() (the window calls it)
            if sna.status().connected:
                break
            time.sleep(0.05)
        try:
            n = sna.take_reference()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                st = sna.status()
                if st.acq_id == n and not st.acquiring:
                    break
                time.sleep(0.05)
            sna.set_sim("dut_inserted", True)
        except Exception:
            pass
    threading.Thread(target=work, name="sim-demo", daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
