"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `amplifier.apply_config()`, so the running brain (local or
remote) re-clamps the gain to the new envelope and pushes it. Groups:

  Amplifier  -- start-up gain and the operating point used by the estimate
  Limits     -- the safety envelope (the gain CEILING lives here)
  Hardware   -- COM port, device gain range/step, P1dB, poll rate
  Appearance -- theme (applies next launch)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtCore, QtWidgets

from ..config import Config
from .theme import COLORS


# ---- little widget builders ------------------------------------------------

def _dspin(value, lo, hi, dec, step, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    w.setLocale(QtCore.QLocale.c())      # "10.5", never "10,5" (gotcha #18)
    w.setRange(lo, hi); w.setDecimals(dec); w.setSingleStep(step)
    w.setValue(value)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _ispin(value, lo, hi, suffix=""):
    w = QtWidgets.QSpinBox()
    w.setLocale(QtCore.QLocale.c())
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
    for group in ("amp", "limits", "hardware", "ui"):
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, amplifier, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = amplifier
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._amp_tab(), "Amplifier")
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

    def _amp_tab(self):
        page, form = self._form_widget()
        a = self.cfg.amp
        self._add(form, "amp", "startup_gain_dB", "Gain at start-up",
                  _dspin(a.startup_gain_dB, 0, 40, 2, 0.5, "dB"))
        self._add(form, "amp", "frequency_Hz", "Signal frequency",
                  _dspin(a.frequency_Hz, 0, 2e10, 0, 1e6, "Hz"))
        self._add(form, "amp", "input_dBm", "Input level",
                  _dspin(a.input_dBm, -120, 30, 2, 0.5, "dBm"))
        form.addRow(_hint("The amplifier always starts OFF; the start-up gain is clamped "
                          "to the limits. Frequency and input level only feed the "
                          "gain / output estimate (the device has no such settings)."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "gain_min_dB", "Gain min",
                  _dspin(lim.gain_min_dB, -10, 40, 2, 0.5, "dB"))
        self._add(form, "limits", "gain_max_dB", "Gain max (safety ceiling)",
                  _dspin(lim.gain_max_dB, -10, 40, 2, 0.5, "dB"))
        self._add(form, "limits", "freq_min_Hz", "Frequency min",
                  _dspin(lim.freq_min_Hz, 0, 2e10, 0, 1e6, "Hz"))
        self._add(form, "limits", "freq_max_Hz", "Frequency max",
                  _dspin(lim.freq_max_Hz, 0, 2e10, 0, 1e6, "Hz"))
        self._add(form, "limits", "input_min_dBm", "Input min",
                  _dspin(lim.input_min_dBm, -150, 30, 1, 1.0, "dBm"))
        self._add(form, "limits", "input_max_dBm", "Input max",
                  _dspin(lim.input_max_dBm, -150, 30, 1, 1.0, "dBm"))
        self._add(form, "limits", "output_warn_dBm", "Warn above output",
                  _dspin(lim.output_warn_dBm, -50, 40, 1, 1.0, "dBm"))
        form.addRow(_hint("Every gain request is clamped to the gain envelope (and to the "
                          "device range). Keep the ceiling low enough that nothing "
                          "downstream -- mixer, sample, detector -- can be overdriven."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "port", "COM port", QtWidgets.QLineEdit(hw.port))
        self._add(form, "hardware", "baud", "Baud rate", _ispin(hw.baud, 1200, 1000000))
        self._add(form, "hardware", "timeout_s", "Read timeout",
                  _dspin(hw.timeout_s, 0.05, 10.0, 2, 0.1, "s"))
        self._add(form, "hardware", "gain_min_dB", "Device gain min",
                  _dspin(hw.gain_min_dB, -10, 40, 2, 0.5, "dB"))
        self._add(form, "hardware", "gain_max_dB", "Device gain max",
                  _dspin(hw.gain_max_dB, -10, 40, 2, 0.5, "dB"))
        self._add(form, "hardware", "gain_step_dB", "Gain step",
                  _dspin(hw.gain_step_dB, 0.01, 5.0, 2, 0.25, "dB"))
        self._add(form, "hardware", "p1db_dBm", "Output P1dB",
                  _dspin(hw.p1db_dBm, -20, 50, 1, 0.5, "dBm"))
        self._add(form, "hardware", "poll_hz", "Poll rate",
                  _dspin(hw.poll_hz, 0.2, 20.0, 1, 0.5, "Hz"))
        btn = QtWidgets.QCheckBox("Give the front-panel buttons back on close")
        btn.setChecked(bool(hw.buttons_on_exit))
        self._add(form, "hardware", "buttons_on_exit", "Front panel", btn)
        form.addRow(_hint("Port and baud are used by the real backend only (restart the "
                          "service to reconnect). Device range and step: GB6000L 0-31 dB "
                          "in 0.5 dB steps per the command list; PA6000L 0.25 dB steps."))
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "dsamp.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()
