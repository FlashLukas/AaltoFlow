"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `cryostat.apply_config()` so the running brain (local or
remote) picks the changes up. Applying settings NEVER moves the magnet or the
temperature; they take effect from the next setpoint. Values are grouped:

  Field / Temperature -- rate, approach, and what counts as "reached"
  Limits              -- the safety envelope every setpoint is clamped to
  Hardware            -- how MultiVu is reached (used by the real backend only)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import FIELD_APPROACHES, TEMPERATURE_APPROACHES, Config
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
    for group in ("field", "temperature", "limits", "hardware", "ui"):
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, cryostat, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = cryostat
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._field_tab(), "Field")
        tabs.addTab(self._temperature_tab(), "Temperature")
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

    def _field_tab(self):
        page, form = self._form_widget()
        f = self.cfg.field
        self._add(form, "field", "rate_mT_per_s", "Ramp rate",
                  _dspin(f.rate_mT_per_s, 0.0, 1000.0, 2, 0.5, "mT/s"))
        self._add(form, "field", "approach", "Approach", self._combo(FIELD_APPROACHES, f.approach))
        self._add(form, "field", "tolerance_mT", "Reached within",
                  _dspin(f.tolerance_mT, 0.0, 100.0, 3, 0.05, "mT"))
        self._add(form, "field", "stable_time_s", "Held for",
                  _dspin(f.stable_time_s, 0.0, 600.0, 1, 0.5, "s"))
        form.addRow(_hint("The field counts as REACHED when it is within the tolerance of the "
                          "setpoint AND MultiVu reports the magnet holding, continuously for the "
                          "hold time. This is the flag a scan waits on. The old LabVIEW program "
                          "used 0.1 mT and a fixed 3 s wait. Rate and approach apply from the "
                          "next setpoint."))
        return page

    def _temperature_tab(self):
        page, form = self._form_widget()
        t = self.cfg.temperature
        self._add(form, "temperature", "rate_K_per_min", "Sweep rate",
                  _dspin(t.rate_K_per_min, 0.0, 100.0, 2, 0.5, "K/min"))
        self._add(form, "temperature", "approach", "Approach",
                  self._combo(TEMPERATURE_APPROACHES, t.approach))
        self._add(form, "temperature", "tolerance_K", "Reached within",
                  _dspin(t.tolerance_K, 0.0, 50.0, 3, 0.05, "K"))
        self._add(form, "temperature", "stable_time_s", "Held for",
                  _dspin(t.stable_time_s, 0.0, 3600.0, 1, 1.0, "s"))
        form.addRow(_hint("REACHED = within the tolerance AND MultiVu says Stable, "
                          "continuously for the hold time. The old program used 0.5 K."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "field_max_mT", "Field max (|B|)",
                  _dspin(lim.field_max_mT, 0.0, 20000.0, 0, 100.0, "mT"))
        self._add(form, "limits", "field_rate_min_mT_per_s", "Field rate min",
                  _dspin(lim.field_rate_min_mT_per_s, 0.0, 1000.0, 3, 0.01, "mT/s"))
        self._add(form, "limits", "field_rate_max_mT_per_s", "Field rate max",
                  _dspin(lim.field_rate_max_mT_per_s, 0.0, 1000.0, 2, 0.5, "mT/s"))
        self._add(form, "limits", "temperature_min_K", "Temperature min",
                  _dspin(lim.temperature_min_K, 0.0, 1000.0, 2, 0.1, "K"))
        self._add(form, "limits", "temperature_max_K", "Temperature max",
                  _dspin(lim.temperature_max_K, 0.0, 1000.0, 1, 1.0, "K"))
        self._add(form, "limits", "temperature_rate_min_K_per_min", "Temperature rate min",
                  _dspin(lim.temperature_rate_min_K_per_min, 0.0, 100.0, 3, 0.01, "K/min"))
        self._add(form, "limits", "temperature_rate_max_K_per_min", "Temperature rate max",
                  _dspin(lim.temperature_rate_max_K_per_min, 0.0, 100.0, 2, 0.5, "K/min"))
        form.addRow(_hint("Every setpoint and rate is clamped to this envelope before it is "
                          "sent. Set the field maximum to YOUR magnet (9, 12 or 14 T). A "
                          "setpoint already outside a new envelope is reported, not re-driven."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "flavor", "MultiVu flavor", QtWidgets.QLineEdit(hw.flavor))
        self._add(form, "hardware", "mpv_port", "MultiPyVu port",
                  _ispin(hw.mpv_port, 1024, 65535))
        sc = QtWidgets.QCheckBox("MultiPyVu simulation (no MultiVu)")
        sc.setChecked(bool(hw.scaffolding))
        self._add(form, "hardware", "scaffolding", "Scaffolding", sc)
        self._add(form, "hardware", "poll_s", "Poll interval",
                  _dspin(hw.poll_s, 0.05, 10.0, 2, 0.1, "s"))
        form.addRow(_hint("Used by the real backend (--real) when the service STARTS: MultiVu "
                          "must run on this PC. The MultiPyVu server listens on 127.0.0.1 only. "
                          "Changes here apply at the next start of the service."))
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "ppms.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()
