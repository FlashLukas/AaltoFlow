"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `synth.apply_config()` so the running brain (local or
remote) picks the changes up. Values are grouped:

  Signal     -- a PRESET (frequency / power / phase / reference), sent only
                when you change it here; the service ADOPTS the unit's state
                at start and changes nothing then. RF is never a setting.
  Limits     -- the safety envelope every setpoint is clamped to
  Hardware   -- USB COM port or Ethernet address, timing, echo tolerances
  Simulator  -- what the simulated unit reports about itself
  Appearance -- light / dark (next launch)

Transport changes (COM port, IP) take effect when the SERVICE restarts: the
connection is opened once, at start.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config, REFERENCES


# ---- little widget builders ------------------------------------------------

def _dspin(value, lo, hi, dec, step, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(lo, hi); w.setDecimals(dec); w.setSingleStep(step)
    w.setValue(value)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _ispin(value, lo, hi, suffix=""):
    w = QtWidgets.QSpinBox()
    w.setRange(lo, hi); w.setValue(int(value))
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _combo(options, current):
    c = QtWidgets.QComboBox()
    c.addItems(list(options))
    c.setCurrentText(str(current))
    return c


def _check(text, value):
    c = QtWidgets.QCheckBox(text)
    c.setChecked(bool(value))
    return c


def _hint(text):
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("hint"); lbl.setWordWrap(True)
    return lbl


def _copy_config_into(dst: Config, src: Config) -> None:
    """Copy every field from src into dst's existing sub-objects IN PLACE, so
    shared references stay valid. Iterates Config._GROUPS so a new group can
    never be forgotten here (gotcha #4)."""
    for group in Config._GROUPS:
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, synth, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = synth
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._signal_tab(), "Signal")
        tabs.addTab(self._limits_tab(), "Limits")
        tabs.addTab(self._hardware_tab(), "Hardware")
        tabs.addTab(self._sim_tab(), "Simulator")
        tabs.addTab(self._appearance_tab(), "Appearance")

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

    # ---- tabs ------------------------------------------------------------

    def _form_widget(self):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setSpacing(8); form.setContentsMargins(16, 16, 16, 16)
        return page, form

    def _add(self, form, group, field, label, widget):
        self.w[(group, field)] = widget
        form.addRow(label, widget)

    def _signal_tab(self):
        page, form = self._form_widget()
        s = self.cfg.signal
        self._add(form, "signal", "frequency_Hz", "Preset frequency",
                  _dspin(s.frequency_Hz, 0, 4e10, 0, 1e6, "Hz"))
        self._add(form, "signal", "power_dBm", "Preset power",
                  _dspin(s.power_dBm, -100, 30, 2, 0.5, "dBm"))
        self._add(form, "signal", "phase_deg", "Preset phase",
                  _dspin(s.phase_deg, 0, 360, 2, 1.0, "deg"))
        self._add(form, "signal", "reference", "10 MHz reference",
                  _combo(REFERENCES, s.reference))
        form.addRow(_hint("Sent to the generator only when you CHANGE a value here and "
                          "press Apply. Nothing is sent at start: the service reads what "
                          "the unit is doing and adopts it. RF on/off is never a preset."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "freq_min_Hz", "Frequency min",
                  _dspin(lim.freq_min_Hz, 0, 4e10, 0, 1e6, "Hz"))
        self._add(form, "limits", "freq_max_Hz", "Frequency max",
                  _dspin(lim.freq_max_Hz, 0, 4e10, 0, 1e6, "Hz"))
        self._add(form, "limits", "power_min_dBm", "Power min",
                  _dspin(lim.power_min_dBm, -100, 30, 1, 1.0, "dBm"))
        self._add(form, "limits", "power_max_dBm", "Power max",
                  _dspin(lim.power_max_dBm, -100, 30, 1, 1.0, "dBm"))
        self._add(form, "limits", "phase_min_deg", "Phase min",
                  _dspin(lim.phase_min_deg, 0, 360, 0, 1.0, "deg"))
        self._add(form, "limits", "phase_max_deg", "Phase max",
                  _dspin(lim.phase_max_deg, 0, 360, 0, 1.0, "deg"))
        # raw counts; the vendor documents no range -- +-30 is a safe guess
        self._add(form, "limits", "vernier_min", "Vernier min",
                  _ispin(lim.vernier_min, -1000, 1000, "counts"))
        self._add(form, "limits", "vernier_max", "Vernier max",
                  _ispin(lim.vernier_max, -1000, 1000, "counts"))
        form.addRow(_hint("Every setpoint is clamped to this envelope AND to the range the "
                          "SG12000L reports about itself, whichever is narrower. Keep the power "
                          "ceiling conservative to protect the sample and the amplifier chain."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "transport", "Transport",
                  _combo(("serial", "tcp"), hw.transport))
        self._add(form, "hardware", "com_port", "COM port (USB)",
                  QtWidgets.QLineEdit(hw.com_port))
        self._add(form, "hardware", "baud", "Baud rate",
                  _ispin(hw.baud, 1200, 1_000_000))
        self._add(form, "hardware", "host", "IP address (Ethernet)",
                  QtWidgets.QLineEdit(hw.host))
        self._add(form, "hardware", "tcp_port", "TCP port",
                  _ispin(hw.tcp_port, 1, 65535))
        self._add(form, "hardware", "timeout_s", "Query timeout",
                  _dspin(hw.timeout_s, 0.05, 30.0, 2, 0.1, "s"))
        self._add(form, "hardware", "poll_hz", "Read-back rate",
                  _dspin(hw.poll_hz, 0.5, 50.0, 1, 1.0, "Hz"))
        self._add(form, "hardware", "power_step_dB", "Attenuator step",
                  _dspin(hw.power_step_dB, 0.0, 5.0, 2, 0.05, "dB"))
        self._add(form, "hardware", "freq_echo_tol_Hz", "Frequency echo tolerance",
                  _dspin(hw.freq_echo_tol_Hz, 0.0, 1e6, 1, 100.0, "Hz"))
        self._add(form, "hardware", "phase_echo_tol_deg", "Phase echo tolerance",
                  _dspin(hw.phase_echo_tol_deg, 0.0, 10.0, 2, 0.1, "deg"))
        self._add(form, "hardware", "phase_mode", "Phase control",
                  _combo(("auto", "on", "off"), hw.phase_mode))
        self._add(form, "hardware", "mute_buzzer", "Buzzer",
                  _check("mute the buzzer (sent when you change it)", hw.mute_buzzer))
        self._add(form, "hardware", "display_off", "Display",
                  _check("switch the OLED off (sent when you change it; back on "
                         "at disconnect)", hw.display_off))
        form.addRow(_hint("USB: the unit is a virtual COM port (115200 8N1). Ethernet: raw "
                          "TCP, port 10001. Transport settings take effect when the service "
                          "restarts. The simulator ignores them."))
        return page

    def _sim_tab(self):
        page, form = self._form_widget()
        sm = self.cfg.sim
        self._add(form, "sim", "freq_min_Hz", "Unit's min frequency",
                  _dspin(sm.freq_min_Hz, 0, 4e10, 0, 1e6, "Hz"))
        self._add(form, "sim", "freq_max_Hz", "Unit's max frequency",
                  _dspin(sm.freq_max_Hz, 0, 4e10, 0, 1e6, "Hz"))
        self._add(form, "sim", "power_min_dBm", "Unit's min power",
                  _dspin(sm.power_min_dBm, -100, 30, 1, 0.5, "dBm"))
        self._add(form, "sim", "power_max_dBm", "Unit's max power",
                  _dspin(sm.power_max_dBm, -100, 30, 1, 0.5, "dBm"))
        self._add(form, "sim", "has_phase", "Phase control",
                  _check("the simulated firmware has PHASE", sm.has_phase))
        self._add(form, "sim", "has_vernier", "Vernier control",
                  _check("the simulated firmware has VERNIER", sm.has_vernier))
        self._add(form, "sim", "external_ref_present", "External reference",
                  _check("a 10 MHz cable is plugged in", sm.external_ref_present))
        # The state the simulated box is in when the service connects -- the
        # module adopts it, exactly as it adopts a real unit's state.
        self._add(form, "sim", "state_rf_on", "Box state: RF",
                  _check("RF already on", sm.state_rf_on))
        self._add(form, "sim", "state_frequency_Hz", "Box state: frequency",
                  _dspin(sm.state_frequency_Hz, 0, 4e10, 0, 1e6, "Hz"))
        self._add(form, "sim", "state_power_dBm", "Box state: power",
                  _dspin(sm.state_power_dBm, -100, 30, 1, 0.5, "dBm"))
        self._add(form, "sim", "state_phase_deg", "Box state: phase",
                  _dspin(sm.state_phase_deg, 0, 360, 2, 1.0, "deg"))
        self._add(form, "sim", "state_vernier", "Box state: vernier",
                  _ispin(sm.state_vernier, -1000, 1000, "counts"))
        self._add(form, "sim", "state_reference", "Box state: reference",
                  _combo(REFERENCES, sm.state_reference))
        form.addRow(_hint("Only used without --real. Takes effect when the simulator restarts. "
                          "'Box state' is what the simulated unit is doing when the service "
                          "connects; the service adopts it."))
        return page

    def _appearance_tab(self):
        page, form = self._form_widget()
        self._add(form, "ui", "theme", "Theme",
                  _combo(("dark", "light"), getattr(self.cfg.ui, "theme", "dark")))
        form.addRow(_hint("Light or dark colour scheme. This is a start-up setting: it takes "
                          "effect the next time you launch the GUI, not immediately."))
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "dssg.ini",
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
