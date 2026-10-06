"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `gen.apply_config()` so the running generator (local or
remote) picks the changes up. Tabs:

  Channel 1 / 2 -- waveform and its numbers (read at start; a value changed
                   here is sent on Apply)
  Limits        -- the lab's safety ceiling per channel (amplitude, peak
                   voltage, frequency): every request is clamped to it
  Coupling      -- CH2 follows CH1, and the phase offset
  Hardware      -- VISA address and behaviour of the real backend
  Appearance    -- light / dark (next launch)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtCore, QtWidgets

from ..config import Config, WAVEFORMS

_GROUPS = ("channel_1", "channel_2", "limits_1", "limits_2", "coupling",
           "hardware", "ui")


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
    def __init__(self, gen, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = gen
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(500)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._channel_tab("channel_1"), "Channel 1")
        tabs.addTab(self._channel_tab("channel_2"), "Channel 2")
        tabs.addTab(self._limits_tab(), "Limits")
        tabs.addTab(self._coupling_tab(), "Coupling")
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
        self._add(form, group, "waveform", "Waveform", _combo(WAVEFORMS, c.waveform))
        self._add(form, group, "frequency_Hz", "Frequency",
                  _dspin(c.frequency_Hz, 0.0, 1e8, 6, 1.0, "Hz"))
        self._add(form, group, "amplitude_Vpp", "Amplitude",
                  _dspin(c.amplitude_Vpp, 0.0, 20.0, 4, 0.01, "Vpp"))
        self._add(form, group, "offset_V", "Offset / DC level",
                  _dspin(c.offset_V, -10.0, 10.0, 4, 0.01, "V"))
        self._add(form, group, "phase_deg", "Phase",
                  _dspin(c.phase_deg, -180.0, 180.0, 2, 1.0, "deg"))
        self._add(form, group, "duty_pct", "Pulse duty",
                  _dspin(c.duty_pct, 0.0, 100.0, 2, 1.0, "%"))
        self._add(form, group, "symmetry_pct", "Ramp symmetry",
                  _dspin(c.symmetry_pct, 0.0, 100.0, 2, 1.0, "%"))
        form.addRow(_hint("Read from the instrument when the service starts (never "
                          "written then). A value you CHANGE here is sent when you "
                          "press Apply; unchanged values are left alone. Volts are "
                          "into the channel's load setting."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        for n in (1, 2):
            group = f"limits_{n}"
            lim = getattr(self.cfg, group)
            self._add(form, group, "amplitude_max_Vpp", f"CH{n} amplitude max",
                      _dspin(lim.amplitude_max_Vpp, 0.0, 20.0, 3, 0.1, "Vpp"))
            self._add(form, group, "peak_max_V", f"CH{n} peak max",
                      _dspin(lim.peak_max_V, 0.0, 10.0, 3, 0.1, "V"))
            self._add(form, group, "freq_max_Hz", f"CH{n} frequency max",
                      _dspin(lim.freq_max_Hz, 0.0, 1e8, 3, 1.0, "Hz"))
        form.addRow(_hint("The lab's ceiling. Every request is clamped to it (and to "
                          "the instrument's own range for the waveform and load). "
                          "PEAK = |offset| + amplitude/2, the highest voltage the "
                          "output ever reaches: lower it on a channel that drives a "
                          "magnet amplifier. A lowered limit is applied at once."))
        return page

    def _coupling_tab(self):
        page, form = self._form_widget()
        co = self.cfg.coupling
        box = QtWidgets.QCheckBox("CH2 takes CH1's frequency and phase (+ offset)")
        box.setChecked(bool(co.ch2_follows_ch1))
        self._add(form, "coupling", "ch2_follows_ch1", "CH2 follows CH1", box)
        self._add(form, "coupling", "phase_offset_deg", "Phase offset",
                  _dspin(co.phase_offset_deg, -180.0, 180.0, 2, 1.0, "deg"))
        form.addRow(_hint("For a synchronous trigger: CH1 drives the experiment, CH2 "
                          "makes a square at the same frequency for the scope's "
                          "trigger input. The channels are re-aligned after every "
                          "frequency change (a short restart of both outputs)."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "visa", "VISA address", QtWidgets.QLineEdit(hw.visa))
        self._add(form, "hardware", "timeout_ms", "Reply timeout",
                  _dspin(hw.timeout_ms, 100, 30000, 0, 100, "ms"))
        self._add(form, "hardware", "poll_hz", "Read-back rate",
                  _dspin(hw.poll_hz, 0.2, 10.0, 1, 0.5, "Hz"))
        self._add(form, "hardware", "phase_unit", "Phase unit (instrument)",
                  _combo(("rad", "deg"), hw.phase_unit))
        form.addRow(_hint("Take effect when the service starts (restart it after a "
                          "change). The VISA address is usually passed by Mission "
                          "Control (--visa). Phase unit: what the AFG's phase command "
                          "speaks -- check once on the instrument (README)."))
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
            else:  # QDoubleSpinBox (an int field stays an int)
                v = float(widget.value())
                setattr(target, field, int(round(v)) if isinstance(getattr(target, field), int)
                        and not isinstance(getattr(target, field), bool) else v)

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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "afg.ini",
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
