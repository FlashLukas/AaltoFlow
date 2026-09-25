"""Settings dialog: edit every tunable, plus load/save config.

Like clMag's Settings pane, nothing here talks to hardware directly -- it edits
the shared `cfg` object in place and then calls `generator.apply_config()` so the
running generator (local or remote) picks the changes up. Values are grouped:

  Signal    -- the power-on defaults (frequency / power / phase / RF on)
  Limits    -- the safety envelope every setpoint is clamped to
  Hardware  -- the GPIB/VISA address and timing (used by the real backend only)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config
from .theme import COLORS


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
    for group in ("signal", "limits", "hardware", "ui"):
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, generator, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = generator
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._signal_tab(), "Signal")
        tabs.addTab(self._limits_tab(), "Limits")
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
        self._add(form, "signal", "frequency_Hz", "Default frequency",
                  _dspin(s.frequency_Hz, 0, 4e10, 0, 1e6, "Hz"))
        self._add(form, "signal", "power_dBm", "Default power",
                  _dspin(s.power_dBm, -200, 40, 2, 0.5, "dBm"))
        self._add(form, "signal", "phase_deg", "Default phase",
                  _dspin(s.phase_deg, -360, 360, 2, 1.0, "deg"))
        rf = QtWidgets.QCheckBox("Enable RF output at start-up")
        rf.setChecked(bool(s.rf_on))
        self._add(form, "signal", "rf_on", "RF at start-up", rf)
        form.addRow(_hint("These are the values pushed to the generator when it starts. "
                          "Leaving RF off at start-up is the safe default."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "freq_min_Hz", "Frequency min",
                  _dspin(lim.freq_min_Hz, 0, 4e10, 0, 1e3, "Hz"))
        self._add(form, "limits", "freq_max_Hz", "Frequency max",
                  _dspin(lim.freq_max_Hz, 0, 4e10, 0, 1e6, "Hz"))
        self._add(form, "limits", "power_min_dBm", "Power min",
                  _dspin(lim.power_min_dBm, -200, 40, 1, 1.0, "dBm"))
        self._add(form, "limits", "power_max_dBm", "Power max",
                  _dspin(lim.power_max_dBm, -200, 40, 1, 1.0, "dBm"))
        self._add(form, "limits", "phase_min_deg", "Phase min",
                  _dspin(lim.phase_min_deg, -720, 720, 0, 1.0, "deg"))
        self._add(form, "limits", "phase_max_deg", "Phase max",
                  _dspin(lim.phase_max_deg, -720, 720, 0, 1.0, "deg"))
        form.addRow(_hint("Every setpoint is clamped to this envelope before it reaches the "
                          "instrument. Match the frequency/power range to your SMB100A's fitted "
                          "options; keep the power ceiling conservative to protect the sample."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "smb_visa", "SMB100A VISA address",
                  QtWidgets.QLineEdit(hw.smb_visa))
        self._add(form, "hardware", "visa_timeout_ms", "VISA timeout",
                  _ispin(hw.visa_timeout_ms, 100, 60000, "ms"))
        self._add(form, "hardware", "settle_s", "Settle after write",
                  _dspin(hw.settle_s, 0.0, 2.0, 3, 0.01, "s"))
        form.addRow(_hint("Used by the real GPIB backend (pyvisa). The simulator ignores these, "
                          "but they are saved to config. Default GPIB0::28::INSTR is the SMB100A's "
                          "factory address."))
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "smb.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()
