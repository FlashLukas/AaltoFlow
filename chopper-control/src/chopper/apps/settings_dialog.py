"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `apply_config()` so the running brain (local or remote)
picks the changes up. Values are grouped:

  Blades    -- which blades are in the lab (the blade control offers only these)
  Limits    -- the safety envelope, on top of the blade's own range
  Lock      -- when the wheel counts as locked (tolerance, hold time, timeouts)
  Hardware  -- the COM port and polling (real backend only)
  Simulator -- the simulated controller's start state and motor
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
    for group in ("blades", "limits", "settle", "hardware", "sim", "ui"):
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, ctrl, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._blades_tab(), "Blades")
        tabs.addTab(self._limits_tab(), "Limits")
        tabs.addTab(self._settle_tab(), "Lock")
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

    def _blades_tab(self):
        page, form = self._form_widget()
        self._add(form, "blades", "owned", "Blades in the lab",
                  QtWidgets.QLineEdit(self.cfg.blades.owned))
        from ..blades import BLADES
        form.addRow(_hint("Comma-separated Thorlabs part numbers. Known: "
                          + ", ".join(BLADES) + ". The blade control offers only "
                          "these (plus whatever the controller currently reports)."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "freq_min_Hz", "Frequency min",
                  _dspin(lim.freq_min_Hz, 0, 20000, 1, 1.0, "Hz"))
        self._add(form, "limits", "freq_max_Hz", "Frequency max",
                  _dspin(lim.freq_max_Hz, 0, 20000, 1, 10.0, "Hz"))
        self._add(form, "limits", "phase_min_deg", "Phase min",
                  _dspin(lim.phase_min_deg, 0, 360, 0, 1.0, "deg"))
        self._add(form, "limits", "phase_max_deg", "Phase max",
                  _dspin(lim.phase_max_deg, 0, 360, 0, 1.0, "deg"))
        form.addRow(_hint("A frequency request is clamped to the blade's own range AND "
                          "this envelope (reported in the log). The defaults restrict "
                          "nothing the blades can do."))
        return page

    def _settle_tab(self):
        page, form = self._form_widget()
        st = self.cfg.settle
        self._add(form, "settle", "tolerance_Hz", "Tolerance",
                  _dspin(st.tolerance_Hz, 0.0, 100.0, 3, 0.1, "Hz"))
        self._add(form, "settle", "tolerance_rel", "Relative tolerance",
                  _dspin(st.tolerance_rel, 0.0, 0.1, 5, 0.0005))
        self._add(form, "settle", "hold_s", "Hold inside tolerance",
                  _dspin(st.hold_s, 0.0, 60.0, 2, 0.1, "s"))
        self._add(form, "settle", "blind_lock_s", "Blind lock time",
                  _dspin(st.blind_lock_s, 0.0, 120.0, 1, 0.5, "s"))
        self._add(form, "settle", "timeout_s", "Scan timeout",
                  _dspin(st.timeout_s, 1.0, 600.0, 0, 5.0, "s"))
        form.addRow(_hint("Locked = measured wheel frequency within max(tolerance, "
                          "relative x f) for the hold time. With REF OUT on 'target' the "
                          "wheel cannot be measured and the blind lock time is used."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "port", "COM port", QtWidgets.QLineEdit(hw.port))
        self._add(form, "hardware", "baud", "Baud rate", _ispin(hw.baud, 1200, 921600))
        self._add(form, "hardware", "timeout_s", "Reply timeout",
                  _dspin(hw.timeout_s, 0.05, 10.0, 2, 0.1, "s"))
        self._add(form, "hardware", "poll_hz", "Poll rate",
                  _dspin(hw.poll_hz, 0.2, 20.0, 1, 0.5, "Hz"))
        stop = QtWidgets.QCheckBox("Put the chopper in standby when the service stops")
        stop.setChecked(bool(hw.stop_on_exit))
        self._add(form, "hardware", "stop_on_exit", "On exit", stop)
        quiet = QtWidgets.QCheckBox("Send verbose=0 when connecting (a write; off = read only)")
        quiet.setChecked(bool(hw.quiet_on_open))
        self._add(form, "hardware", "quiet_on_open", "On connect", quiet)
        form.addRow(_hint("Used by the real backend (USB virtual COM port, 115200 8N1). "
                          "The port applies at the next start of the service."))
        return page

    def _sim_tab(self):
        page, form = self._form_widget()
        sm = self.cfg.sim
        self._add(form, "sim", "spinup_tau_s", "Spin-up time constant",
                  _dspin(sm.spinup_tau_s, 0.01, 30.0, 2, 0.1, "s"))
        self._add(form, "sim", "coast_tau_s", "Coast-down time constant",
                  _dspin(sm.coast_tau_s, 0.01, 60.0, 2, 0.1, "s"))
        self._add(form, "sim", "jitter_rel", "Frequency jitter (rms, relative)",
                  _dspin(sm.jitter_rel, 0.0, 0.05, 5, 0.0001))
        self._add(form, "sim", "external_input_Hz", "Signal on EXT REF IN",
                  _dspin(sm.external_input_Hz, 0.0, 20000.0, 1, 10.0, "Hz"))
        self._add(form, "sim", "blade", "Start blade", QtWidgets.QLineEdit(sm.blade))
        self._add(form, "sim", "ref_mode", "Start reference in", QtWidgets.QLineEdit(sm.ref_mode))
        self._add(form, "sim", "output_mode", "Start reference out",
                  QtWidgets.QLineEdit(sm.output_mode))
        self._add(form, "sim", "frequency_Hz", "Start frequency",
                  _dspin(sm.frequency_Hz, 0.0, 20000.0, 1, 10.0, "Hz"))
        self._add(form, "sim", "phase_deg", "Start phase",
                  _dspin(sm.phase_deg, 0.0, 360.0, 0, 1.0, "deg"))
        run = QtWidgets.QCheckBox("Simulated chopper is found running")
        run.setChecked(bool(sm.enabled))
        self._add(form, "sim", "enabled", "Start running", run)
        form.addRow(_hint("Only the simulator uses these. The physics values apply at "
                          "once; the start state at the next start."))
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "chopper.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()
