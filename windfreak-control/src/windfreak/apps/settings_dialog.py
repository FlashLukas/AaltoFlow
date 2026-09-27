"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `synth.apply_config()` so the running synthesizer (local or
remote) picks the changes up. Tabs:

  Channel A / B -- the start-up frequency / power / phase of each output
  Reference     -- which clock the PLLs use
  Limits        -- the safety envelope every setpoint is clamped to
  Hardware      -- COM port and behaviour of the real backend
  Appearance    -- light / dark (next launch)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtCore, QtWidgets

from ..config import Config, REFERENCE_SOURCES

_GROUPS = ("channel_a", "channel_b", "reference", "limits", "hardware", "ui")


# ---- little widget builders ------------------------------------------------

def _dspin(value, lo, hi, dec, step, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    # C locale: "2500.0", never "2500,0" or "2,500.0" (gotcha #18)
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    w.setLocale(loc)
    w.setRange(lo, hi); w.setDecimals(dec); w.setSingleStep(step)
    w.setValue(value)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _combo(options, current):
    w = QtWidgets.QComboBox()
    w.addItems(list(options))
    w.setCurrentText(str(current))
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
    def __init__(self, synth, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = synth
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(500)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._channel_tab("channel_a"), "Channel A")
        tabs.addTab(self._channel_tab("channel_b"), "Channel B")
        tabs.addTab(self._reference_tab(), "Reference")
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

    def _channel_tab(self, group):
        page, form = self._form_widget()
        c = getattr(self.cfg, group)
        self._add(form, group, "frequency_Hz", "Start frequency",
                  _dspin(c.frequency_Hz, 0, 3e10, 1, 1e6, "Hz"))
        self._add(form, group, "power_dBm", "Start power",
                  _dspin(c.power_dBm, -100, 30, 2, 0.5, "dBm"))
        self._add(form, group, "phase_deg", "Start phase",
                  _dspin(c.phase_deg, 0, 360, 2, 1.0, "deg"))
        form.addRow(_hint("Programmed when the service starts. The RF output always "
                          "starts OFF -- there is deliberately no setting for that."))
        return page

    def _reference_tab(self):
        page, form = self._form_widget()
        r = self.cfg.reference
        self._add(form, "reference", "source", "Reference",
                  _combo(REFERENCE_SOURCES, r.source))
        self._add(form, "reference", "ext_MHz", "External frequency",
                  _dspin(r.ext_MHz, 1, 200, 3, 1.0, "MHz"))
        form.addRow(_hint("The clock for BOTH PLLs. With 'external', a 10-100 MHz "
                          "signal must be on REF IN at exactly the declared frequency, "
                          "or neither channel locks."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        rows = (("freq_min_Hz", "Frequency min", 0, 3e10, 0, 1e6, "Hz"),
                ("freq_max_Hz", "Frequency max", 0, 3e10, 0, 1e6, "Hz"),
                ("power_min_dBm", "Power min", -100, 30, 1, 1.0, "dBm"),
                ("power_max_dBm", "Power max", -100, 30, 1, 1.0, "dBm"),
                ("phase_min_deg", "Phase min", 0, 360, 1, 1.0, "deg"),
                ("phase_max_deg", "Phase max", 0, 360, 1, 1.0, "deg"),
                ("ext_ref_min_MHz", "Ext. reference min", 1, 200, 3, 1.0, "MHz"),
                ("ext_ref_max_MHz", "Ext. reference max", 1, 200, 3, 1.0, "MHz"))
        for field, label, lo, hi, dec, step, unit in rows:
            self._add(form, "limits", field, label,
                      _dspin(getattr(lim, field), lo, hi, dec, step, unit))
        form.addRow(_hint("Every setpoint is clamped to this envelope before it reaches "
                          "the instrument. Lower the power ceiling if something fragile "
                          "sits downstream. Above 20 GHz the output is uncalibrated."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "port", "COM port", QtWidgets.QLineEdit(hw.port))
        self._add(form, "hardware", "timeout_s", "Reply timeout",
                  _dspin(hw.timeout_s, 0.1, 10.0, 2, 0.1, "s"))
        self._add(form, "hardware", "poll_hz", "Status poll rate",
                  _dspin(hw.poll_hz, 0.5, 20.0, 1, 1.0, "Hz"))
        pll = QtWidgets.QCheckBox("Power the PLL down when RF is off (quietest)")
        pll.setChecked(bool(hw.pll_off_when_rf_off))
        self._add(form, "hardware", "pll_off_when_rf_off", "RF off mode", pll)
        self._add(form, "hardware", "phase_command", "Phase command",
                  _combo(("relative", "absolute"), hw.phase_command))
        self._add(form, "hardware", "channel_spacing_Hz", "Channel spacing",
                  _dspin(hw.channel_spacing_Hz, 0.0, 1000.0, 1, 1.0, "Hz"))
        self._add(form, "hardware", "temp_warn_C", "Temperature warning",
                  _dspin(hw.temp_warn_C, 20.0, 90.0, 1, 1.0, "C"))
        form.addRow(_hint("Port, timeout, poll rate, RF off mode, phase command and "
                          "channel spacing take effect when the service starts (restart "
                          "it after a change). Channel spacing 0 keeps the instrument's "
                          "own setting."))
        return page

    def _appearance_tab(self):
        page, form = self._form_widget()
        self._add(form, "ui", "theme", "Theme",
                  _combo(("dark", "light"), getattr(self.cfg.ui, "theme", "dark")))
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
            else:
                widget.setValue(val)

    # ---- actions ---------------------------------------------------------

    def _apply_and_close(self):
        self._pull_into_cfg()
        self.ctrl.apply_config()
        self.on_applied()
        self.accept()

    def _save_config(self):
        self._pull_into_cfg()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "windfreak.ini",
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
