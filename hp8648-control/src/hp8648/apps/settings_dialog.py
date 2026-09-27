"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `source.apply_config()` so the running brain (local or
remote) picks the changes up and re-clamps. Values are grouped:

  Signal     -- the start-up frequency and level (RF always starts OFF)
  Limits     -- the safety envelope every setpoint is clamped to
  Hardware   -- GPIB address, options, timing (VISA fields: real backend only)
  Appearance -- the theme (applies next launch)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtCore, QtWidgets

from ..config import Config


# ---- little widget builders ------------------------------------------------

def _c(w):
    """C locale on number widgets (gotcha #18): '.' decimal, no separators."""
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    w.setLocale(loc)
    return w


def _dspin(value, lo, hi, dec, step, suffix=""):
    w = _c(QtWidgets.QDoubleSpinBox())
    w.setRange(lo, hi); w.setDecimals(dec); w.setSingleStep(step)
    w.setValue(value)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _ispin(value, lo, hi, suffix=""):
    w = _c(QtWidgets.QSpinBox())
    w.setRange(lo, hi); w.setValue(int(value))
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _check(text, value):
    w = QtWidgets.QCheckBox(text)
    w.setChecked(bool(value))
    return w


def _hint(text):
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("hint"); lbl.setWordWrap(True)
    return lbl


def _copy_config_into(dst: Config, src: Config) -> None:
    """Copy every field from src into dst's existing sub-objects IN PLACE, so
    shared references stay valid (the simulator holds cfg.hardware)."""
    for group in Config._GROUPS:
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, source, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = source
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._signal_tab(), "Signal")
        tabs.addTab(self._limits_tab(), "Limits")
        tabs.addTab(self._hardware_tab(), "Hardware")
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
        self._add(form, "signal", "frequency_Hz", "Start-up frequency",
                  _dspin(s.frequency_Hz, 0, 4.1e9, 0, 1e6, "Hz"))
        self._add(form, "signal", "power_dBm", "Start-up level",
                  _dspin(s.power_dBm, -140, 25, 1, 1.0, "dBm"))
        form.addRow(_hint("Pushed to the generator when the service starts. The RF "
                          "output always starts OFF; switching it on is always a "
                          "deliberate command."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "freq_min_Hz", "Frequency min",
                  _dspin(lim.freq_min_Hz, 0, 4.1e9, 0, 1e3, "Hz"))
        self._add(form, "limits", "freq_max_Hz", "Frequency max",
                  _dspin(lim.freq_max_Hz, 0, 4.1e9, 0, 1e6, "Hz"))
        self._add(form, "limits", "power_min_dBm", "Level min",
                  _dspin(lim.power_min_dBm, -140, 25, 1, 1.0, "dBm"))
        self._add(form, "limits", "power_max_dBm", "Level max",
                  _dspin(lim.power_max_dBm, -140, 25, 1, 1.0, "dBm"))
        self._add(form, "limits", "enforce_spec_ceiling", "Spec ceiling",
                  _check("Also clamp to the specified max at the current frequency",
                         lim.enforce_spec_ceiling))
        form.addRow(_hint("Every setpoint is clamped to this envelope before it reaches "
                          "the instrument. The 8648D is specified to +13 dBm up to "
                          "2500 MHz and +10 dBm above; keep 'Level max' lower to "
                          "protect the sample or a downstream amplifier."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "visa_resource", "VISA address",
                  QtWidgets.QLineEdit(hw.visa_resource))
        self._add(form, "hardware", "visa_timeout_ms", "VISA timeout",
                  _ispin(hw.visa_timeout_ms, 100, 60000, "ms"))
        self._add(form, "hardware", "option_1ea", "Option 1EA",
                  _check("High-power option fitted", hw.option_1ea))
        self._add(form, "hardware", "reset_on_open", "Reset at connect",
                  _check("Send *RST at connect (RF off, modulation off)", hw.reset_on_open))
        self._add(form, "hardware", "poll_s", "Read-back period",
                  _dspin(hw.poll_s, 0.02, 5.0, 2, 0.05, "s"))
        self._add(form, "hardware", "switch_settle_s", "Switching wait",
                  _dspin(hw.switch_settle_s, 0.0, 2.0, 3, 0.01, "s"))
        form.addRow(_hint("The VISA fields are used by the real GPIB backend only (the "
                          "8648's factory HP-IB address is 19). 'Switching wait' is how "
                          "long the brain waits after a frequency or level change "
                          "before reading back: the spec switching time is < 75 ms "
                          "below 1001 MHz and < 100 ms above."))
        return page

    def _appearance_tab(self):
        page, form = self._form_widget()
        combo = QtWidgets.QComboBox()
        combo.addItems(["dark", "light"])
        combo.setCurrentText(getattr(self.cfg.ui, "theme", "dark"))
        self._add(form, "ui", "theme", "Theme", combo)
        form.addRow(_hint("Light or dark colour scheme. A start-up setting: it takes "
                          "effect the next time you launch the GUI."))
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "hp8648.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()
