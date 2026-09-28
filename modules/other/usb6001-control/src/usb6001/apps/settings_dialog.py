"""Settings dialog: every tunable of the USB-6001 module, plus load/save.

Nothing here talks to the hardware: the widgets are copied into the shared
`cfg` object IN PLACE on Apply, then `daq.apply_config()` is called, so the
running brain (local, or the service over the network) picks them up.

Tabs:
  AI         per channel: enabled, name, terminal, unit + linear scale;
             samples per reading and the sample rate
  AO         per output: name and the safety limits
  DIO        per line: direction in/out/unused, name, initial and safe state
  Limits     the card's own envelope
  Hardware   device name, driver, poll rate, simulator options
  Appearance theme

WHAT APPLIES WHEN: AI enabled/terminal and the line DIRECTIONS decide which
DAQmx tasks exist, so they apply at the next SERVICE START (the service saves
its .ini on Apply, so the restart picks them up). Everything else at once.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..config import (AI_CHANNELS, AO_CHANNELS, DIO_LINES, DIRECTIONS, LEVELS,
                      TERMINALS, Config)
from ..net.protocol import apply_config_dict, config_to_dict


def _dspin(lo, hi, dec, step, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    w.setLocale(QtCore.QLocale.c())         # "1.5", not "1,5" (gotcha #18)
    w.setRange(lo, hi); w.setDecimals(dec); w.setSingleStep(step)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _ispin(lo, hi, suffix=""):
    w = QtWidgets.QSpinBox()
    w.setRange(lo, hi)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _combo(options):
    w = QtWidgets.QComboBox()
    w.addItems(list(options))
    return w


def _hint(text):
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("hint"); lbl.setWordWrap(True)
    return lbl


def _header(grid, titles):
    for c, t in enumerate(titles):
        lbl = QtWidgets.QLabel(t)
        lbl.setObjectName("sectionLabel")
        grid.addWidget(lbl, 0, c)


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, daq, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = daq
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(720)

        #: (group, index or None, field) -> widget
        self.w: dict = {}

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._ai_tab(), "AI")
        tabs.addTab(self._ao_tab(), "AO")
        tabs.addTab(self._dio_tab(), "DIO")
        tabs.addTab(self._limits_tab(), "Limits")
        tabs.addTab(self._hardware_tab(), "Hardware")
        tabs.addTab(self._appearance_tab(), "Appearance")
        self._populate(cfg)

        bar = QtWidgets.QHBoxLayout()
        load_btn = QtWidgets.QPushButton("Load config..."); load_btn.clicked.connect(self._load_config)
        save_btn = QtWidgets.QPushButton("Save config..."); save_btn.clicked.connect(self._save_config)
        bar.addWidget(load_btn); bar.addWidget(save_btn); bar.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        apply = QtWidgets.QPushButton("Apply"); apply.setObjectName("primary")
        apply.clicked.connect(self._apply_and_close)
        bar.addWidget(cancel); bar.addWidget(apply)

        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(tabs)
        root.addLayout(bar)

    # ---- tabs ----------------------------------------------------------------------

    def _page(self):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(10)
        return page, lay

    def _ai_tab(self):
        page, lay = self._page()
        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(8)
        _header(grid, ("", "On", "Name", "Terminal", "Unit", "Slope (unit/V)", "Offset (unit)"))
        for i, ch in enumerate(AI_CHANNELS):
            grid.addWidget(QtWidgets.QLabel(ch), i + 1, 0)
            row = [("enabled", QtWidgets.QCheckBox()), ("name", QtWidgets.QLineEdit()),
                   ("terminal", _combo(TERMINALS)), ("unit", QtWidgets.QLineEdit()),
                   ("slope", _dspin(-1e9, 1e9, 6, 0.1)), ("offset", _dspin(-1e9, 1e9, 6, 0.1))]
            for c, (f, wdg) in enumerate(row):
                self.w[("ai", i, f)] = wdg
                grid.addWidget(wdg, i + 1, c + 1)
        lay.addLayout(grid)
        form = QtWidgets.QFormLayout()
        self.w[("ai", None, "samples_per_read")] = _ispin(1, 100000, "samples")
        self.w[("ai", None, "rate_Hz")] = _dspin(0.1, 20000, 1, 100, "Hz / channel")
        form.addRow("One reading averages", self.w[("ai", None, "samples_per_read")])
        form.addRow("Sample rate", self.w[("ai", None, "rate_Hz")])
        lay.addLayout(form)
        lay.addWidget(_hint("DIFF uses two pins: ai0 with ai4, ai1 with ai5, ai2 with ai6, "
                            "ai3 with ai7 -- the partner is switched off. The card shares "
                            "20 kS/s between all enabled channels. 'On' and 'Terminal' apply "
                            "after a SERVICE RESTART; names and scales at once."))
        lay.addStretch(1)
        return page

    def _ao_tab(self):
        page, lay = self._page()
        grid = QtWidgets.QGridLayout()
        _header(grid, ("", "Name", "Min", "Max"))
        for i, ch in enumerate(AO_CHANNELS):
            grid.addWidget(QtWidgets.QLabel(ch), i + 1, 0)
            for c, (f, wdg) in enumerate((("name", QtWidgets.QLineEdit()),
                                           ("min_V", _dspin(-10, 10, 4, 0.1, "V")),
                                           ("max_V", _dspin(-10, 10, 4, 0.1, "V")))):
                self.w[("ao", i, f)] = wdg
                grid.addWidget(wdg, i + 1, c + 1)
        lay.addLayout(grid)
        lay.addWidget(_hint("Every set_ao is clamped to these limits (with a warning). "
                            "A known output outside new limits is moved inside them on Apply."))
        lay.addStretch(1)
        return page

    def _dio_tab(self):
        page, lay = self._page()
        grid = QtWidgets.QGridLayout(); grid.setVerticalSpacing(3)
        _header(grid, ("", "Direction", "Name", "At start (outputs)", "On clean stop (outputs)"))
        for i, line in enumerate(DIO_LINES):
            grid.addWidget(QtWidgets.QLabel(line.upper()), i + 1, 0)
            for c, (f, wdg) in enumerate((("direction", _combo(DIRECTIONS)),
                                           ("name", QtWidgets.QLineEdit()),
                                           ("initial", _combo(LEVELS)),
                                           ("safe_state", _combo(LEVELS)))):
                self.w[("dio", i, f)] = wdg
                grid.addWidget(wdg, i + 1, c + 1)
        lay.addLayout(grid)
        lay.addWidget(_hint("Directions apply after a SERVICE RESTART, never live: a line must "
                            "not switch from input to driven output during a measurement. "
                            "'At start' = leave writes nothing (the line keeps what it has); "
                            "low/high writes that level once when the service starts. "
                            "'On clean stop' is written only when the service is stopped "
                            "normally, not when it is killed."))
        lay.addStretch(1)
        return page

    def _limits_tab(self):
        page, lay = self._page()
        form = QtWidgets.QFormLayout()
        for f, label, wdg in (
                ("ao_hw_min_V", "AO card minimum", _dspin(-10, 10, 3, 0.1, "V")),
                ("ao_hw_max_V", "AO card maximum", _dspin(-10, 10, 3, 0.1, "V")),
                ("ai_aggregate_max_Hz", "AI aggregate rate", _dspin(1, 1e6, 0, 1000, "S/s")),
                ("samples_max", "Max samples per reading", _ispin(1, 1000000)),
                ("read_time_max_s", "Max time per reading", _dspin(0.001, 10, 3, 0.1, "s"))):
            self.w[("limits", None, f)] = wdg
            form.addRow(label, wdg)
        lay.addLayout(form)
        lay.addWidget(_hint("The USB-6001's own envelope (NI spec sheet). Rarely changed."))
        lay.addStretch(1)
        return page

    def _hardware_tab(self):
        page, lay = self._page()
        form = QtWidgets.QFormLayout()
        for f, label, wdg in (
                ("device", "DAQmx device (NI MAX)", QtWidgets.QLineEdit()),
                ("driver", "Driver", _combo(("sim", "nidaq"))),
                ("poll_hz", "Live readings per second", _dspin(0.1, 100, 1, 1, "Hz")),
                ("timeout_s", "Read timeout", _dspin(0.1, 60, 1, 0.5, "s")),
                ("sim_ai_loopback", "Simulator: ai0/ai1 read ao0/ao1", QtWidgets.QCheckBox()),
                ("sim_di_loopback", "Simulator: inputs read outputs", QtWidgets.QCheckBox())):
            self.w[("hardware", None, f)] = wdg
            form.addRow(label, wdg)
        lay.addLayout(form)
        lay.addWidget(_hint("Device name, driver and simulator options apply at the next "
                            "service start. The card is claimed by its SERIAL number, so a "
                            "second service on the same card is refused."))
        lay.addStretch(1)
        return page

    def _appearance_tab(self):
        page, lay = self._page()
        form = QtWidgets.QFormLayout()
        self.w[("ui", None, "theme")] = _combo(("dark", "light"))
        form.addRow("Theme", self.w[("ui", None, "theme")])
        lay.addLayout(form)
        lay.addWidget(_hint("Applies at the next GUI start."))
        lay.addStretch(1)
        return page

    # ---- widgets <-> config -------------------------------------------------------------

    def _target(self, cfg: Config, group: str, idx):
        grp = getattr(cfg, group)
        if idx is None:
            return grp
        return (grp.lines if group == "dio" else grp.channels)[idx]

    def _populate(self, cfg: Config) -> None:
        for (group, idx, f), wdg in self.w.items():
            val = getattr(self._target(cfg, group, idx), f)
            if isinstance(wdg, QtWidgets.QCheckBox):
                wdg.setChecked(bool(val))
            elif isinstance(wdg, QtWidgets.QComboBox):
                wdg.setCurrentText(str(val))
            elif isinstance(wdg, QtWidgets.QLineEdit):
                wdg.setText(str(val))
            elif isinstance(wdg, QtWidgets.QSpinBox):
                wdg.setValue(int(val))
            else:
                wdg.setValue(float(val))

    def _collect(self, cfg: Config) -> None:
        for (group, idx, f), wdg in self.w.items():
            if isinstance(wdg, QtWidgets.QCheckBox):
                val = wdg.isChecked()
            elif isinstance(wdg, QtWidgets.QComboBox):
                val = wdg.currentText()
            elif isinstance(wdg, QtWidgets.QLineEdit):
                val = wdg.text().strip()
            else:
                val = wdg.value()
            setattr(self._target(cfg, group, idx), f, val)

    # ---- buttons -------------------------------------------------------------------------

    def _apply_and_close(self):
        self._collect(self.cfg)
        self.ctrl.apply_config()          # local brain, or pushed to the service
        self.on_applied()
        self.accept()

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            self._populate(Config.load(path))

    def _save_config(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "usb6001.ini",
                                                        "Config (*.ini)")
        if path:
            tmp = Config()
            apply_config_dict(tmp, config_to_dict(self.cfg))
            self._collect(tmp)
            tmp.save(path)
