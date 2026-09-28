"""Settings dialog: the tunables that are not on the front panel, plus load/save.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `smu.apply_config()` so the running brain (local or
remote) re-clamps and re-sends everything. The live source/measure settings
(level, limit, ranges, NPLC, 4-wire) are on the main window; this dialog holds:

  Limits     -- the safety envelope every setpoint is clamped to
  Source     -- how long a new level must hold before it counts as settled
  Acquire    -- readings per acquisition, client timeout
  Hardware   -- VISA address, library, terminals, mains frequency (real backend)
  Simulator  -- the pretend sample on the simulator's leads
  Appearance -- dark / light (next launch)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtCore, QtWidgets

from ..config import Config

_GROUPS = ("source", "measure", "acquisition", "limits", "hardware", "sim", "ui")


def _dspin(value, lo, hi, dec, step, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    w.setLocale(QtCore.QLocale.c())          # a decimal POINT (gotcha #18)
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSingleStep(step)
    w.setValue(value)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _ispin(value, lo, hi, suffix=""):
    w = QtWidgets.QSpinBox()
    w.setLocale(QtCore.QLocale.c())
    w.setRange(lo, hi)
    w.setValue(int(value))
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _combo(options, current):
    w = QtWidgets.QComboBox()
    w.addItems(options)
    w.setCurrentText(str(current))
    return w


def _hint(text):
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


def _copy_config_into(dst: Config, src: Config) -> None:
    """Copy every field from src into dst's existing sub-objects IN PLACE, so
    shared references stay valid (the simulator holds cfg.sim, for one)."""
    for group in _GROUPS:
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, smu, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = smu
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(500)
        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._limits_tab(), "Limits")
        tabs.addTab(self._source_tab(), "Source")
        tabs.addTab(self._hardware_tab(), "Hardware")
        tabs.addTab(self._sim_tab(), "Simulator")
        tabs.addTab(self._appearance_tab(), "Appearance")

        bar = QtWidgets.QHBoxLayout()
        load_btn = QtWidgets.QPushButton("Load config...")
        load_btn.clicked.connect(self._load_config)
        save_btn = QtWidgets.QPushButton("Save config...")
        save_btn.clicked.connect(self._save_config)
        bar.addWidget(load_btn)
        bar.addWidget(save_btn)
        bar.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        apply = QtWidgets.QPushButton("Apply")
        apply.setObjectName("primary")
        apply.clicked.connect(self._apply_and_close)
        bar.addWidget(cancel)
        bar.addWidget(apply)

        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(tabs)
        root.addLayout(bar)

    # ---- tabs ------------------------------------------------------------

    def _form_widget(self):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setSpacing(8)
        form.setContentsMargins(16, 16, 16, 16)
        return page, form

    def _add(self, form, group, field, label, widget):
        self.w[(group, field)] = widget
        form.addRow(label, widget)

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "voltage_max_V", "Voltage max (+-)",
                  _dspin(lim.voltage_max_V, 0.02, 210, 3, 1.0, "V"))
        self._add(form, "limits", "current_max_A", "Current max (+-)",
                  _dspin(lim.current_max_A, 1e-8, 1.05, 9, 0.001, "A"))
        self._add(form, "limits", "current_limit_min_A", "Smallest current limit",
                  _dspin(lim.current_limit_min_A, 1e-9, 1e-3, 9, 1e-9, "A"))
        self._add(form, "limits", "voltage_limit_min_V", "Smallest voltage limit",
                  _dspin(lim.voltage_limit_min_V, 0.0, 10.0, 3, 0.01, "V"))
        self._add(form, "limits", "nplc_min", "NPLC min",
                  _dspin(lim.nplc_min, 0.01, 10, 2, 0.01))
        self._add(form, "limits", "nplc_max", "NPLC max",
                  _dspin(lim.nplc_max, 0.01, 10, 2, 0.1))
        self._add(form, "limits", "readings_max", "Readings per acquisition max",
                  _ispin(lim.readings_max, 1, 1000000))
        form.addRow(_hint("Your safety envelope: every setpoint is clamped to it before "
                          "it reaches the instrument. Lower the voltage/current maxima for "
                          "a delicate sample. The 2450's own output boxes (21 V x 1.05 A, "
                          "210 V x 105 mA) are always enforced on top."))
        return page

    def _source_tab(self):
        page, form = self._form_widget()
        self._add(form, "source", "settle_s", "Settle after a new level",
                  _dspin(self.cfg.source.settle_s, 0.0, 60.0, 3, 0.01, "s"))
        a = self.cfg.acquisition
        self._add(form, "acquisition", "timeout_s", "Acquisition timeout",
                  _dspin(a.timeout_s, 1.0, 3600.0, 1, 1.0, "s"))
        form.addRow(_hint("A scan waits this long after each new source level (with the "
                          "output on) before it takes readings. Raise it for a slow sample: "
                          "a gate, a long cable, a capacitive junction."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "visa_resource", "VISA resource",
                  QtWidgets.QLineEdit(hw.visa_resource))
        self._add(form, "hardware", "visa_library", "VISA library",
                  QtWidgets.QLineEdit(hw.visa_library))
        self._add(form, "hardware", "visa_timeout_ms", "VISA timeout",
                  _ispin(hw.visa_timeout_ms, 500, 120000, "ms"))
        self._add(form, "hardware", "terminals", "Terminals",
                  _combo(["front", "rear"], hw.terminals))
        self._add(form, "hardware", "line_freq_Hz", "Mains frequency",
                  _dspin(hw.line_freq_Hz, 50, 60, 0, 10, "Hz"))
        self._add(form, "hardware", "poll_hz", "Reading rate (max)",
                  _dspin(hw.poll_hz, 1, 100, 1, 1, "Hz"))
        form.addRow(_hint("Used by the real backend (--real). VISA library empty = NI-VISA / "
                          "Keysight; '@py' = pyvisa-py. The instrument must be in the SCPI "
                          "command set (MENU > System > Settings)."))
        return page

    def _sim_tab(self):
        page, form = self._form_widget()
        s = self.cfg.sim
        self._add(form, "sim", "load", "Pretend sample",
                  _combo(["resistor", "diode", "open"], s.load))
        self._add(form, "sim", "resistance_ohm", "Resistor",
                  _dspin(s.resistance_ohm, 1e-3, 1e12, 3, 100.0, "ohm"))
        self._add(form, "sim", "lead_resistance_ohm", "Lead resistance",
                  _dspin(s.lead_resistance_ohm, 0.0, 1e3, 3, 0.1, "ohm"))
        self._add(form, "sim", "diode_rs_ohm", "Diode series R",
                  _dspin(s.diode_rs_ohm, 0.0, 1e4, 3, 1.0, "ohm"))
        self._add(form, "sim", "diode_n", "Diode ideality n",
                  _dspin(s.diode_n, 1.0, 5.0, 2, 0.1))
        self._add(form, "sim", "noise_ppm", "Noise at 1 NPLC",
                  _dspin(s.noise_ppm, 0.0, 1e5, 1, 5.0, "ppm of range"))
        form.addRow(_hint("Only the simulator reads these. Lead resistance shows up in a "
                          "2-wire reading and drops out in 4-wire."))
        return page

    def _appearance_tab(self):
        page, form = self._form_widget()
        self._add(form, "ui", "theme", "Theme",
                  _combo(["dark", "light"], getattr(self.cfg.ui, "theme", "dark")))
        form.addRow(_hint("Light or dark colour scheme. A start-up setting: it takes effect "
                          "the next time you launch the GUI."))
        return page

    # ---- read widgets back into cfg -------------------------------------

    def _pull_into_cfg(self):
        for (group, field), widget in self.w.items():
            target = getattr(self.cfg, group)
            if isinstance(widget, QtWidgets.QCheckBox):
                setattr(target, field, bool(widget.isChecked()))
            elif isinstance(widget, QtWidgets.QLineEdit):
                setattr(target, field, widget.text().strip())
            elif isinstance(widget, QtWidgets.QComboBox):
                setattr(target, field, widget.currentText())
            elif isinstance(widget, QtWidgets.QSpinBox):
                setattr(target, field, int(widget.value()))
            else:  # QDoubleSpinBox
                setattr(target, field, float(widget.value()))

    def _refresh_widgets_from_cfg(self):
        for (group, field), widget in self.w.items():
            val = getattr(getattr(self.cfg, group), field)
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(bool(val))
            elif isinstance(widget, QtWidgets.QLineEdit):
                widget.setText(str(val))
            elif isinstance(widget, QtWidgets.QComboBox):
                widget.setCurrentText(str(val))
            elif isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                widget.setValue(val)

    # ---- actions ---------------------------------------------------------

    def _apply_and_close(self):
        self._pull_into_cfg()
        self.ctrl.apply_config()
        self.on_applied()
        self.accept()

    def _save_config(self):
        self._pull_into_cfg()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "k2450.ini",
                                                        "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()
