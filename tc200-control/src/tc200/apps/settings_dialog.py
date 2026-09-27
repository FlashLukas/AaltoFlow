"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `heater.apply_config()` so the running brain (local or
remote) picks the changes up. Applying NEVER changes the setpoint or switches
the heater; the Controller tab's values are pushed to the TC200, and only the
ones that differ from what the box already has. Values are grouped:

  Temperature -- what counts as "reached", and the pessimistic rate a scan's
                 timeout is derived from
  Controller  -- settings stored IN the TC200 (sensor, PID, PMAX, TMAX)
  Limits      -- the safety envelope every setpoint is clamped to
  Hardware    -- serial port and start/stop behaviour (real backend)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import (D_GAIN_RANGE, I_GAIN_RANGE, P_GAIN_RANGE, PMAX_MAX_W, PMAX_MIN_W,
                      SENSORS, TMAX_MAX_C, TMAX_MIN_C, TSET_MAX_C, TSET_MIN_C, Config)

#: every config group this dialog edits (gotcha #4: a new group goes here too)
_GROUPS = ("temperature", "device", "limits", "hardware", "ui")


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
    for group in _GROUPS:
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, heater, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = heater
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._temperature_tab(), "Temperature")
        tabs.addTab(self._device_tab(), "Controller")
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

    @staticmethod
    def _combo(options, value):
        w = QtWidgets.QComboBox()
        w.addItems(list(options))
        w.setCurrentText(str(value))
        return w

    def _temperature_tab(self):
        page, form = self._form_widget()
        t = self.cfg.temperature
        self._add(form, "temperature", "tolerance_C", "Reached within",
                  _dspin(t.tolerance_C, 0.0, 20.0, 2, 0.05, "°C"))
        self._add(form, "temperature", "stable_time_s", "Held for",
                  _dspin(t.stable_time_s, 0.0, 3600.0, 1, 1.0, "s"))
        self._add(form, "temperature", "slowest_rate_C_per_min", "Slowest rate",
                  _dspin(t.slowest_rate_C_per_min, 0.01, 100.0, 2, 0.5, "°C/min"))
        form.addRow(_hint("REACHED = heater on, no sensor alarm, and the reading within the "
                          "tolerance of the setpoint continuously for the hold time. This is "
                          "the flag a scan waits on. The slowest rate only sets how long a scan "
                          "may wait for one point: cooling is passive and slow."))
        return page

    def _device_tab(self):
        page, form = self._form_widget()
        d = self.cfg.device
        self._add(form, "device", "sensor", "Sensor type", self._combo(SENSORS, d.sensor))
        self._add(form, "device", "p_gain", "P gain", _ispin(d.p_gain, *P_GAIN_RANGE))
        self._add(form, "device", "i_gain", "I gain", _ispin(d.i_gain, *I_GAIN_RANGE))
        self._add(form, "device", "d_gain", "D gain", _ispin(d.d_gain, *D_GAIN_RANGE))
        self._add(form, "device", "pmax_W", "Power limit (PMAX)",
                  _dspin(d.pmax_W, PMAX_MIN_W, PMAX_MAX_W, 1, 0.5, "W"))
        self._add(form, "device", "tmax_C", "Trip (TMAX)",
                  _dspin(d.tmax_C, TMAX_MIN_C, TMAX_MAX_C, 1, 1.0, "°C"))
        form.addRow(_hint("These live IN the TC200 and were read from it. Apply pushes the "
                          "values you changed (the sensor only while the heater is off). The "
                          "sensor MUST match the one wired -- a PT100 here -- or the controller "
                          "misreads the temperature. Manual recipe: P 125, I and D 0, then a "
                          "little I (< 10) to remove the offset."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "temperature_min_C", "Setpoint min",
                  _dspin(lim.temperature_min_C, TSET_MIN_C, TSET_MAX_C, 1, 1.0, "°C"))
        self._add(form, "limits", "temperature_max_C", "Setpoint max",
                  _dspin(lim.temperature_max_C, TSET_MIN_C, TSET_MAX_C, 1, 1.0, "°C"))
        self._add(form, "limits", "tmax_margin_C", "Stay below TMAX by",
                  _dspin(lim.tmax_margin_C, 0.0, 50.0, 1, 0.5, "°C"))
        self._add(form, "limits", "pmax_max_W", "Power limit max",
                  _dspin(lim.pmax_max_W, PMAX_MIN_W, PMAX_MAX_W, 1, 0.5, "W"))
        form.addRow(_hint("Every setpoint is clamped to min .. min(max, TMAX - margin). A "
                          "setpoint AT TMAX trips the relay on the first overshoot. Set the "
                          "power limit max to the heater's rating."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "port", "Serial port", QtWidgets.QLineEdit(hw.port))
        self._add(form, "hardware", "baud", "Baud", _ispin(hw.baud, 1200, 921600))
        self._add(form, "hardware", "timeout_s", "Reply timeout",
                  _dspin(hw.timeout_s, 0.05, 10.0, 2, 0.1, "s"))
        self._add(form, "hardware", "poll_s", "Poll interval",
                  _dspin(hw.poll_s, 0.05, 10.0, 2, 0.1, "s"))
        self._add(form, "hardware", "settings_poll_s", "Re-read stored settings",
                  _dspin(hw.settings_poll_s, 0.5, 600.0, 1, 1.0, "s"))
        self._add(form, "hardware", "stat_base", "Status byte base",
                  self._combo(["16", "10"], hw.stat_base))
        self._add(form, "hardware", "expected_sensor", "Sensor wired",
                  self._combo(SENSORS, hw.expected_sensor))
        push = QtWidgets.QCheckBox("Push the Controller tab at start (else adopt)")
        push.setChecked(bool(hw.push_on_start))
        self._add(form, "hardware", "push_on_start", "At start", push)
        off = QtWidgets.QCheckBox("Switch the heater OFF when the service stops")
        off.setChecked(bool(hw.disable_on_shutdown))
        self._add(form, "hardware", "disable_on_shutdown", "At stop", off)
        form.addRow(_hint("Port settings are used by the real backend (--real) when the "
                          "service STARTS. Starting never switches the heater or changes the "
                          "setpoint."))
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
            old = getattr(target, field)
            if isinstance(widget, QtWidgets.QCheckBox):
                setattr(target, field, bool(widget.isChecked()))
            elif isinstance(widget, QtWidgets.QLineEdit):
                setattr(target, field, widget.text().strip())
            elif isinstance(widget, QtWidgets.QComboBox):
                text = widget.currentText()
                setattr(target, field, int(text) if isinstance(old, int) else text)
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "tc200.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()
