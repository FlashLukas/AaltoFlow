"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `brain.apply_config()` so the running phase shifter (local
or remote) picks the changes up. Values are grouped:

  Signal    -- the carrier at start (phase/att/output are read from the unit)
  Limits    -- the safety envelope every setpoint is clamped to
  Device    -- what this unit can do (step sizes, optional frequency command)
  Hardware  -- COM port and timing (used by the real backend only)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config


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


def _hint(text):
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("hint"); lbl.setWordWrap(True)
    return lbl


def _copy_config_into(dst: Config, src: Config) -> None:
    """Copy every field from src into dst's existing sub-objects IN PLACE, so
    shared references stay valid."""
    for group in ("signal", "limits", "device", "hardware", "ui"):
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, brain, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = brain
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._signal_tab(), "Signal")
        tabs.addTab(self._limits_tab(), "Limits")
        tabs.addTab(self._device_tab(), "Device")
        tabs.addTab(self._hardware_tab(), "Hardware")
        tabs.addTab(self._appearance_tab(), "Appearance")

        bar = QtWidgets.QHBoxLayout()
        load_btn = QtWidgets.QPushButton("Load config…"); load_btn.clicked.connect(self._load_config)
        save_btn = QtWidgets.QPushButton("Save config…"); save_btn.clicked.connect(self._save_config)
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
        self._add(form, "signal", "frequency_MHz", "Carrier at start",
                  _dspin(s.frequency_MHz, 0, 100000, 1, 10.0, "MHz"))
        form.addRow(_hint("Phase, attenuation and RF on/off are READ from the phase "
                          "shifter when the service starts and adopted as they are -- "
                          "nothing is sent to the unit at start. The carrier cannot be "
                          "read back, so it starts from this value (it is not sent at "
                          "start either)."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "phase_min_deg", "Phase min",
                  _dspin(lim.phase_min_deg, -720, 720, 1, 1.0, "deg"))
        self._add(form, "limits", "phase_max_deg", "Phase max",
                  _dspin(lim.phase_max_deg, -720, 720, 1, 1.0, "deg"))
        self._add(form, "limits", "att_min_dB", "Attenuation min",
                  _dspin(lim.att_min_dB, 0, 60, 2, 0.25, "dB"))
        self._add(form, "limits", "att_max_dB", "Attenuation max",
                  _dspin(lim.att_max_dB, 0, 60, 2, 0.25, "dB"))
        self._add(form, "limits", "freq_min_MHz", "Carrier min",
                  _dspin(lim.freq_min_MHz, 0, 100000, 1, 10.0, "MHz"))
        self._add(form, "limits", "freq_max_MHz", "Carrier max",
                  _dspin(lim.freq_max_MHz, 0, 100000, 1, 10.0, "MHz"))
        form.addRow(_hint("Every setpoint is clamped to this envelope. Phase is periodic: "
                          "the envelope may be wider than the device's -180..+180, the "
                          "service wraps before sending. Raise 'Attenuation min' to cap "
                          "the power that leaves the box."))
        return page

    def _device_tab(self):
        page, form = self._form_widget()
        dev = self.cfg.device
        self._add(form, "device", "model", "Model", QtWidgets.QLineEdit(dev.model))
        self._add(form, "device", "phase_step_deg", "Phase step",
                  _dspin(dev.phase_step_deg, 0.001, 90, 3, 0.5, "deg"))
        self._add(form, "device", "att_step_dB", "Attenuator step",
                  _dspin(dev.att_step_dB, 0.001, 10, 3, 0.25, "dB"))
        self._add(form, "device", "freq_command", "Frequency command",
                  QtWidgets.QLineEdit(dev.freq_command))
        form.addRow(_hint("PS6000L: 0.5 deg and 0.25 dB. The frequency command is EMPTY "
                          "because the PS6000L command list has none; if your firmware "
                          "has one, enter it with {mhz} as the value, e.g. "
                          "FREQ {mhz:.3f}MHZ."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "port", "COM port", QtWidgets.QLineEdit(hw.port))
        self._add(form, "hardware", "baud", "Baud rate", _ispin(hw.baud, 1200, 921600))
        self._add(form, "hardware", "timeout_s", "Read timeout",
                  _dspin(hw.timeout_s, 0.05, 10.0, 2, 0.1, "s"))
        self._add(form, "hardware", "poll_hz", "Read-back rate",
                  _dspin(hw.poll_hz, 0.5, 50.0, 1, 1.0, "Hz"))
        form.addRow(_hint("Used by the real USB backend (pyserial). The COM port is the one "
                          "Windows assigned (Device Manager > Ports); the unit talks at "
                          "115200 baud. Port changes apply at the next service start."))
        return page

    def _appearance_tab(self):
        page, form = self._form_widget()
        combo = QtWidgets.QComboBox()
        combo.addItems(["dark", "light"])
        combo.setCurrentText(getattr(self.cfg.ui, "theme", "dark"))
        self._add(form, "ui", "theme", "Theme", combo)
        form.addRow(_hint("Light or dark colour scheme. This is a start-up setting — it takes "
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "dsphase.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()
