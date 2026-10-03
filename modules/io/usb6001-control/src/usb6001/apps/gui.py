"""Control GUI for the NI USB-6001 general-purpose DAQ.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator (demo layout)
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: the window holds a Daq-like object (the in-process
Daq, or an Usb6001Client facade for a remote service). It sends commands
(set_ao, set_do, read_ai) and reads a status snapshot on a Qt timer. Events
arrive on a Qt signal so they cross safely into the GUI thread.

The layout is modelled on clMag's AUX I/O tab: AO spin + Set per channel, live
AI readouts (volts and the configured unit), and one row per digital line
showing its DIRECTION -- a lamp for an input, a checkbox for an output, a dash
for an unused line. Directions are not editable here on purpose: they change
in Settings > DIO and apply after a service restart.

The signature indicator is DaqIndicator: the card drawn as a box with eight AI
bars, two AO needles and thirteen LEDs coloured by direction.
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import AI_CHANNELS, AO_CHANNELS, DIO_LINES, Config
from ..sim_system import build_sim_system, demo_config
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always
from .theme import COLORS, apply_palette, build_stylesheet, set_theme


class Bridge(QtCore.QObject):
    """Carries brain events across the thread boundary into the GUI."""
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


def _muted(text: str, size: int = 11, wrap: bool = False) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(text)
    lbl.setStyleSheet(f"color:{COLORS['muted']}; font-size:{size}px;")
    lbl.setWordWrap(wrap)        # long notes wrap instead of widening the window
    return lbl


def _fmt(v, fmt="{:+.4f}") -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "--"
    return fmt.format(f) if math.isfinite(f) else "--"


# ------------------------------------------------------------- the indicator

class DaqIndicator(QtWidgets.QWidget):
    """The card as a small box: 8 AI bars (bipolar, +-10 V), 2 AO needles, 13 LEDs.

    LED colours: an INPUT is green when high, an OUTPUT amber when high, both
    hollow when low; an unused line is a grey ring; an output whose level is
    not known yet (never written, card cannot read it back) shows a '?'.
    Everything is read from COLORS at paint time, so it follows the theme.
    """

    def __init__(self):
        super().__init__()
        self.setFixedHeight(150)
        self.setMinimumWidth(560)
        self._ai = [float("nan")] * 8
        self._ai_on = [False] * 8
        self._ao = [float("nan")] * 2
        self._ao_known = [False, False]
        self._dio = [None] * 13
        self._dir = ["unused"] * 13
        self._ok = True

    def set_state(self, s) -> None:
        self._ai = list(s.ai_V)
        self._ai_on = list(s.ai_enabled)
        self._ao = list(s.ao_V)
        self._ao_known = list(s.ao_known)
        self._dio = list(s.dio)
        self._dir = list(s.dio_dir)
        self._ok = bool(s.connected) and not s.hw_error
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        C = lambda k, a=255: QtGui.QColor(COLORS[k]) if a == 255 else _alpha(COLORS[k], a)
        w, h = self.width(), self.height()
        body = QtCore.QRectF(2, 6, w - 4, h - 12)
        p.setPen(QtGui.QPen(C("border"), 1.5))
        p.setBrush(C("panel_hi"))
        p.drawRoundedRect(body, 12, 12)
        f = p.font(); f.setPointSize(7); f.setBold(True); p.setFont(f)

        # ---- 8 analog inputs: bipolar bars around a 0 V line -----------------
        x0, top, bot = 20.0, 24.0, h - 34.0
        mid = (top + bot) / 2
        for i in range(8):
            x = x0 + i * 22
            p.setPen(QtGui.QPen(C("border"), 1))
            p.setBrush(C("panel"))
            p.drawRoundedRect(QtCore.QRectF(x, top, 12, bot - top), 3, 3)
            v = self._ai[i]
            if self._ai_on[i] and isinstance(v, (int, float)) and math.isfinite(v):
                frac = max(-1.0, min(1.0, v / 10.0))
                hbar = frac * (bot - top) / 2
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(C("accent") if self._ok else C("muted"))
                p.drawRect(QtCore.QRectF(x + 2, mid - max(hbar, 0), 8, abs(hbar)))
            p.setPen(QtGui.QPen(C("muted"), 1))
            p.drawLine(QtCore.QPointF(x - 1, mid), QtCore.QPointF(x + 13, mid))
            p.setPen(C("text") if self._ai_on[i] else C("muted"))
            p.drawText(QtCore.QRectF(x - 6, bot + 4, 24, 12), QtCore.Qt.AlignHCenter, f"ai{i}")
        p.setPen(C("muted"))
        p.drawText(QtCore.QRectF(x0, 8, 176, 14), QtCore.Qt.AlignLeft, "ANALOG IN  +-10 V")

        # ---- 2 analog outputs: needle gauges -----------------------------------
        gx = x0 + 8 * 22 + 24
        for k in range(2):
            cx = gx + 46 + k * 96
            cy = h * 0.62
            r = 36.0
            rect = QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r)
            p.setPen(QtGui.QPen(C("border"), 5, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawArc(rect, 0, 180 * 16)
            known = self._ao_known[k] and math.isfinite(float(self._ao[k]))
            if known:
                frac = max(-1.0, min(1.0, float(self._ao[k]) / 10.0))
                ang = math.radians(90 - frac * 90)       # -10 V left, +10 V right
                tip = QtCore.QPointF(cx + (r - 6) * math.cos(ang), cy - (r - 6) * math.sin(ang))
                p.setPen(QtGui.QPen(C("accent"), 2.6, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
                p.drawLine(QtCore.QPointF(cx, cy), tip)
                label = f"{float(self._ao[k]):+.2f} V"
            else:
                p.setPen(QtGui.QPen(C("muted"), 1.6, QtCore.Qt.DashLine))
                p.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(cx, cy - r + 8))
                label = "unknown"
            p.setBrush(C("text")); p.setPen(QtCore.Qt.NoPen)
            p.drawEllipse(QtCore.QPointF(cx, cy), 3, 3)
            p.setPen(C("text") if known else C("muted"))
            p.drawText(QtCore.QRectF(cx - 45, cy + 6, 90, 12), QtCore.Qt.AlignHCenter,
                       f"ao{k}  {label}")
        p.setPen(C("muted"))
        p.drawText(QtCore.QRectF(gx + 10, 8, 180, 14), QtCore.Qt.AlignLeft, "ANALOG OUT")

        # ---- 13 digital lines: LEDs grouped by port ----------------------------------
        lx = gx + 2 * 96 + 34
        p.setPen(C("muted"))
        p.drawText(QtCore.QRectF(lx, 8, 260, 14), QtCore.Qt.AlignLeft, "DIGITAL  P0 / P1 / P2")
        ports = [(0, range(0, 8)), (1, range(8, 12)), (2, range(12, 13))]
        for row, (port, idxs) in enumerate(ports):
            y = 36 + row * 30
            p.setPen(C("muted"))
            p.drawText(QtCore.QRectF(lx, y - 6, 22, 12), QtCore.Qt.AlignLeft, f"P{port}")
            for col, i in enumerate(idxs):
                c = QtCore.QPointF(lx + 30 + col * 22, y)
                d, lvl = self._dir[i], self._dio[i]
                key = "ok" if d == "in" else ("accent" if d == "out" else "border")
                if d == "unused":
                    p.setPen(QtGui.QPen(C("border"), 1.5)); p.setBrush(QtCore.Qt.NoBrush)
                elif lvl:
                    p.setPen(QtGui.QPen(C(key), 1.5)); p.setBrush(C(key))
                else:
                    p.setPen(QtGui.QPen(C(key), 1.5)); p.setBrush(C(key, 40))
                p.drawEllipse(c, 7, 7)
                if d == "out" and lvl is None:
                    p.setPen(C("text"))
                    p.drawText(QtCore.QRectF(c.x() - 7, c.y() - 7, 14, 14),
                               QtCore.Qt.AlignCenter, "?")
        # legend
        ly = h - 26
        for j, (txt, key) in enumerate((("in", "ok"), ("out", "accent"), ("unused", "border"))):
            cx = lx + 30 + j * 70
            p.setPen(QtGui.QPen(C(key), 1.5)); p.setBrush(C(key) if key != "border" else QtCore.Qt.NoBrush)
            p.drawEllipse(QtCore.QPointF(cx, ly + 5), 4, 4)
            p.setPen(C("muted"))
            p.drawText(QtCore.QRectF(cx + 8, ly - 1, 60, 12), QtCore.Qt.AlignLeft, txt)
        p.end()


def _alpha(hexcolor: str, a: int) -> QtGui.QColor:
    c = QtGui.QColor(hexcolor)
    c.setAlpha(a)
    return c


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self.setWindowTitle("NI USB-6001 · General DAQ" + ("  (remote)" if remote else ""))
        self.resize(1280, 860)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)
        # Control or viewer (control_bar.py): a bar above everything, only for
        # a GUI on a service whose client knows about control -- a local GUI
        # owns its card and has nobody to share it with.
        self._control_bar = None
        if remote and hasattr(self.ctrl, "take_control"):
            central = QtWidgets.QWidget(); central.setObjectName("root")
            vbox = QtWidgets.QVBoxLayout(central)
            vbox.setContentsMargins(0, 0, 0, 0); vbox.setSpacing(0)
            self._control_bar = ControlBar(self.ctrl, self, log=self._on_event)
            vbox.addWidget(self._control_bar)
            vbox.addWidget(root, 1)
            self.setCentralWidget(central)
        else:
            self.setCentralWidget(root)

        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        self.ctrl.start()
        self._layout_dirs = None         # rebuild the DIO rows when the active layout changes
        self._sync_ao_inputs()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

        # The first GUI to connect gets control; a later one opens as a viewer
        # (control_bar.py). Only once the log exists, so the bar can say so.
        # No output is marked "always": this DAQ has no safety verb
        # (net/service.py) -- only Settings and the read button are.
        if self._control_bar is not None:
            self._control_bar.claim_if_free()

    # ---- layout --------------------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(360)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(14)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("USB-6001")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        ccard, clay = _card()
        top = QtWidgets.QHBoxLayout()
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        top.addWidget(self.conn_dot); top.addStretch(1)
        self.reads_label = _muted("")
        top.addWidget(self.reads_label)
        clay.addLayout(top)
        self.idn_label = _muted("--")
        self.idn_label.setWordWrap(True)
        clay.addWidget(self.idn_label)
        self.err_label = QtWidgets.QLabel("")
        self.err_label.setWordWrap(True)
        self.err_label.setStyleSheet(f"color:{COLORS['danger']}; font-weight:600; font-size:11px;")
        self.err_label.hide()
        clay.addWidget(self.err_label)
        self.pending_label = QtWidgets.QLabel(
            "Saved layout differs from the running one: restart the service to apply it.")
        self.pending_label.setWordWrap(True)
        self.pending_label.setStyleSheet(f"color:{COLORS['accent']}; font-size:11px; font-weight:600;")
        self.pending_label.hide()
        clay.addWidget(self.pending_label)
        col.addWidget(ccard)

        # analog outputs
        acard, alay = _card("Analog outputs")
        self.ao_spin, self.ao_now, self.ao_name = [], [], []
        for i in range(2):
            name = QtWidgets.QLabel("")
            name.setStyleSheet("font-weight:700;")
            alay.addWidget(name)
            row = QtWidgets.QHBoxLayout()
            spin = QtWidgets.QDoubleSpinBox()
            spin.setDecimals(4); spin.setSingleStep(0.1); spin.setSuffix("  V")
            spin.setLocale(QtCore.QLocale.c())          # "1.5", not "1,5" (gotcha #18)
            btn = QtWidgets.QPushButton("Set"); btn.setObjectName("primary")
            btn.clicked.connect(lambda _=False, k=i: self._set_ao(k))
            row.addWidget(spin, 1); row.addWidget(btn)
            alay.addLayout(row)
            now = _muted("")
            alay.addWidget(now)
            self.ao_name.append(name); self.ao_spin.append(spin); self.ao_now.append(now)
        alay.addWidget(_muted("The USB-6001 cannot read its outputs back: after a restart "
                              "an output shows 'unknown' until you set it. Nothing is "
                              "written at start.", 10, wrap=True))
        self._apply_ao_limits()
        col.addWidget(acard)

        # fresh reading
        rcard, rlay = _card("Fresh reading")
        rrow = QtWidgets.QHBoxLayout()
        read_btn = QtWidgets.QPushButton("Read inputs now")
        read_btn.clicked.connect(self._read_now)
        mark_always(read_btn)        # read_ai only reads: fine for a viewer
        rrow.addWidget(read_btn); rrow.addStretch(1)
        rlay.addLayout(rrow)
        self.sample_label = _muted("No sample yet. A scan reads inputs this way: "
                                   "every value taken after the trigger.", 10, wrap=True)
        self.sample_label.setWordWrap(True)
        rlay.addWidget(self.sample_label)
        col.addWidget(rcard)
        col.addStretch(1)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(14)

        icard, ilay = _card("Card")
        self.indicator = DaqIndicator()
        ilay.addWidget(self.indicator)
        colw.addWidget(icard)

        row = QtWidgets.QHBoxLayout(); row.setSpacing(14)
        # analog inputs
        aicard, ailay = _card("Analog inputs (live)")
        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(14); grid.setVerticalSpacing(4)
        for c, t in enumerate(("CH", "NAME", "VOLTS", "SCALED")):
            lbl = _muted(t, 10); lbl.setStyleSheet(lbl.styleSheet() + "font-weight:700;")
            grid.addWidget(lbl, 0, c)
        self.ai_rows = []
        for i in range(8):
            ch = QtWidgets.QLabel(AI_CHANNELS[i]); ch.setStyleSheet("font-weight:700;")
            name = QtWidgets.QLabel(""); volts = QtWidgets.QLabel("--"); scaled = QtWidgets.QLabel("--")
            for lbl in (volts, scaled):
                lbl.setStyleSheet("font-family:Consolas, 'DejaVu Sans Mono', monospace; font-size:14px;")
                lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                lbl.setMinimumWidth(110)
            for c, wdg in enumerate((ch, name, volts, scaled)):
                grid.addWidget(wdg, i + 1, c)
            self.ai_rows.append((ch, name, volts, scaled))
        grid.setColumnStretch(1, 1)
        ailay.addLayout(grid)
        ailay.addStretch(1)
        row.addWidget(aicard, 1)

        # digital lines
        dcard, dlay = _card("Digital lines")
        self.dio_grid = QtWidgets.QGridLayout()
        self.dio_grid.setHorizontalSpacing(12); self.dio_grid.setVerticalSpacing(2)
        dlay.addLayout(self.dio_grid)
        dlay.addWidget(_muted("Directions change in Settings > DIO and apply after a "
                              "service restart (a line never flips from input to "
                              "driven output mid-measurement).", 10, wrap=True))
        dlay.addStretch(1)
        self.dio_rows: list = []
        row.addWidget(dcard, 1)
        colw.addLayout(row, 3)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(110)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _build_dio_rows(self, dirs) -> None:
        """(Re)build one row per digital line for the ACTIVE directions."""
        while self.dio_grid.count():
            item = self.dio_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for c, t in enumerate(("LINE", "NAME", "DIR", "STATE")):
            lbl = _muted(t, 10); lbl.setStyleSheet(lbl.styleSheet() + "font-weight:700;")
            self.dio_grid.addWidget(lbl, 0, c)
        self.dio_rows = []
        for i, line in enumerate(DIO_LINES):
            d = dirs[i]
            ln = QtWidgets.QLabel(line.upper()); ln.setStyleSheet("font-weight:700;")
            name = QtWidgets.QLabel(self.cfg.dio.lines[i].name if self.cfg.dio.lines[i].name.upper() != line.upper() else "")
            colour = COLORS["ok"] if d == "in" else (COLORS["accent"] if d == "out" else COLORS["muted"])
            dl = QtWidgets.QLabel(d.upper())
            dl.setStyleSheet(f"color:{colour}; font-weight:700; font-size:11px;")
            if d == "out":
                state = QtWidgets.QCheckBox("")
                # .clicked fires on a USER click only (gotcha #13), so the
                # refresh timer's setChecked cannot send a command back.
                state.clicked.connect(lambda checked, k=i: self._set_do(k, checked))
            elif d == "in":
                state = QtWidgets.QLabel("●")
                state.setStyleSheet(f"color:{COLORS['border']}; font-size:16px;")
            else:
                state = _muted("-")
            for c, wdg in enumerate((ln, name, dl, state)):
                self.dio_grid.addWidget(wdg, i + 1, c)
            self.dio_rows.append((d, state))
        self.dio_grid.setColumnStretch(1, 1)

    # ---- actions ---------------------------------------------------------------------

    def _apply_ao_limits(self):
        for i in range(2):
            ch = self.cfg.ao.channels[i]
            self.ao_spin[i].setRange(float(ch.min_V), float(ch.max_V))
            self.ao_name[i].setText(f"{AO_CHANNELS[i]}  {ch.name}  "
                                    f"({float(ch.min_V):g} .. {float(ch.max_V):g} V)")

    def _sync_ao_inputs(self):
        """Put the spin boxes on the known output values (none right after a
        restart), so pressing Set without typing does not change anything."""
        try:
            s = self.ctrl.status()
            for i in range(2):
                if s.ao_known[i]:
                    self.ao_spin[i].setValue(float(s.ao_V[i]))
        except Exception:
            pass

    def _set_ao(self, i: int):
        try:
            self.ctrl.set_ao(i, self.ao_spin[i].value())
        except Exception as exc:
            self._on_event("error", f"ao{i}: {exc}")

    def _set_do(self, i: int, state: bool):
        try:
            self.ctrl.set_do(i, bool(state))
        except Exception as exc:
            self._on_event("error", f"{DIO_LINES[i]}: {exc}")

    def _read_now(self):
        try:
            r = self.ctrl.read_ai()
        except Exception as exc:
            self._on_event("error", f"read: {exc}")
            return
        parts = [f"{k} {_fmt(v, '{:+.4f}')} {r['units'].get(k, 'V')}"
                 for k, v in r.get("values", {}).items()]
        self.sample_label.setText(f"#{r.get('acq_id')}: " + ("   ".join(parts) or "no inputs enabled"))

    def _open_settings(self):
        self.ctrl.get_config()          # fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        self._apply_ao_limits()
        self._layout_dirs = None        # names may have changed: rebuild the rows

    # ---- refresh & events ---------------------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
                            f'<span style="color:{color}">{msg}</span>')

    def _refresh(self):
        if self._control_bar is not None:
            self._control_bar.refresh()
        s = self.ctrl.status()
        if s.connected:
            self.conn_dot.setText("●  connected")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        self.reads_label.setText(f"{s.reads} reads · {_fmt(s.read_ms, '{:.0f}')} ms")
        if s.idn:
            self.idn_label.setText(f"{s.idn}  ·  {self.cfg.hardware.device}")
        self.err_label.setVisible(bool(s.hw_error))
        if s.hw_error:
            self.err_label.setText(f"HARDWARE READ FAILED: {s.hw_error} (values shown are the last good ones)")
        self.pending_label.setVisible(bool(s.restart_pending))

        chans = self.cfg.ai.channels
        for i, (ch, name, volts, scaled) in enumerate(self.ai_rows):
            on = bool(s.ai_enabled[i])
            name.setText(chans[i].name if on else f"{chans[i].name}  (off)")
            name.setStyleSheet("" if on else f"color:{COLORS['muted']};")
            volts.setText(f"{_fmt(s.ai_V[i])} V" if on else "--")
            unit = chans[i].unit.strip() or "V"
            scaled.setText(f"{_fmt(s.ai[i], '{:+.4g}')} {unit}" if on else "--")

        for i in range(2):
            if s.ao_known[i]:
                self.ao_now[i].setText(f"output now: {float(s.ao_V[i]):+.4f} V")
            else:
                self.ao_now[i].setText("output now: unknown (not set this session)")

        dirs = list(s.dio_dir)
        if dirs != self._layout_dirs:
            self._layout_dirs = dirs
            self._build_dio_rows(dirs)
        for i, (d, w) in enumerate(self.dio_rows):
            lvl = s.dio[i]
            if d == "out":
                w.blockSignals(True)
                w.setChecked(bool(lvl))
                w.blockSignals(False)
                w.setText("?  unknown" if lvl is None else ("high" if lvl else "low"))
            elif d == "in":
                col = COLORS["ok"] if lvl else COLORS["border"]
                w.setStyleSheet(f"color:{col}; font-size:16px;")

        self.indicator.set_state(s)

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Daq-like object. The theme is chosen ONCE here,
    from cfg.ui.theme, BEFORE any widget is built (gotcha #6)."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())
    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """The LOCAL simulator with the demo layout (sim_system.demo_config).
    `theme` (if given) overrides cfg.ui.theme for this launch."""
    cfg = demo_config()
    if theme:
        cfg.ui.theme = theme
    daq, _ = build_sim_system(cfg)
    return run_app(daq, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
