"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `laser.apply_config()` so the running brain (local or
remote) picks the changes up and re-clamps everything. Tabs:

  Presets   -- values sent to the laser when CHANGED here (never at start)
  Limits    -- the safety envelope every request is clamped to
  Filters   -- the AOTF crystal table (names, ranges, NKT crystal numbers)
  Hardware  -- COM port, module addresses, watchdog (real backend only)
  Appearance -- theme (next launch)
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtCore, QtWidgets

from ..config import Config

#: every config group; a new group goes here too (gotcha #4)
GROUPS = ("startup", "limits", "filters", "hardware", "ui")


def _dspin(value, lo, hi, dec, step, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    w.setLocale(QtCore.QLocale.c())       # gotcha #18
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


def _check(value, text=""):
    w = QtWidgets.QCheckBox(text)
    w.setChecked(bool(value))
    return w


def _hint(text):
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("hint"); lbl.setWordWrap(True)
    return lbl


def _copy_config_into(dst: Config, src: Config) -> None:
    """Copy every field from src into dst's existing sub-objects IN PLACE, so
    shared references stay valid."""
    for group in GROUPS:
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, laser, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = laser
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._startup_tab(), "Presets")
        tabs.addTab(self._limits_tab(), "Limits")
        tabs.addTab(self._filters_tab(), "Filters")
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

    def _startup_tab(self):
        page, form = self._form_widget()
        s = self.cfg.startup
        self._add(form, "startup", "power_pct", "Power level",
                  _dspin(s.power_pct, 0, 100, 1, 1.0, "%"))
        self._add(form, "startup", "filter", "Crystal", QtWidgets.QLineEdit(s.filter))
        self._add(form, "startup", "wavelengths_nm", "Line wavelengths (nm)",
                  QtWidgets.QLineEdit(s.wavelengths_nm))
        self._add(form, "startup", "amplitudes_pct", "Line amplitudes (%)",
                  QtWidgets.QLineEdit(s.amplitudes_pct))
        form.addRow(_hint("NOT applied at start: the service reads the laser and "
                          "adopts what it is doing. A value you CHANGE here is sent "
                          "when you press Apply (clamped like any request). Eight "
                          "comma-separated values, line 1 first. No setting can "
                          "switch emission on."))
        return page

    def _limits_tab(self):
        page, form = self._form_widget()
        lim = self.cfg.limits
        self._add(form, "limits", "power_min_pct", "Power level min",
                  _dspin(lim.power_min_pct, 0, 100, 1, 1.0, "%"))
        self._add(form, "limits", "power_max_pct", "Power level max",
                  _dspin(lim.power_max_pct, 0, 100, 1, 1.0, "%"))
        self._add(form, "limits", "amplitude_max_pct", "RF amplitude max",
                  _dspin(lim.amplitude_max_pct, 0, 100, 1, 1.0, "%"))
        form.addRow(_hint("Every request is clamped to this envelope (and the clamp "
                          "is logged). Raise the power ceiling deliberately."))
        return page

    def _filters_tab(self):
        page, form = self._form_widget()
        f = self.cfg.filters
        self._add(form, "filters", "names", "Names", QtWidgets.QLineEdit(f.names))
        self._add(form, "filters", "min_nm", "Min (nm)", QtWidgets.QLineEdit(f.min_nm))
        self._add(form, "filters", "max_nm", "Max (nm)", QtWidgets.QLineEdit(f.max_nm))
        self._add(form, "filters", "crystal", "Crystal no. (NKT)",
                  QtWidgets.QLineEdit(f.crystal))
        form.addRow(_hint("One entry per AOTF crystal, comma-separated, same order in "
                          "every row. The single RF driver drives one crystal at a "
                          "time; its range limits every line. On the real laser the "
                          "range the driver reports wins. Crystal no.: 1, 2 = the "
                          "slots of the SELECT with the lower bus address, 3, 4 = "
                          "the other SELECT."))
        return page

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "port", "COM port", QtWidgets.QLineEdit(hw.port))
        self._add(form, "hardware", "dll_path", "NKTPDLL.dll path",
                  QtWidgets.QLineEdit(hw.dll_path))
        self._add(form, "hardware", "autodetect", "Find modules",
                  _check(hw.autodetect, "by module type code"))
        self._add(form, "hardware", "extreme_addr", "EXTREME address",
                  _ispin(hw.extreme_addr, 1, 255))
        self._add(form, "hardware", "rf_addr", "RF driver address",
                  _ispin(hw.rf_addr, 1, 255))
        self._add(form, "hardware", "watchdog_s", "Laser watchdog",
                  _ispin(hw.watchdog_s, 0, 255, "s"))
        self._add(form, "hardware", "poll_hz", "Poll rate",
                  _dspin(hw.poll_hz, 0.5, 20, 1, 0.5, "Hz"))
        self._add(form, "hardware", "client_timeout_s", "Lost-client guard",
                  _dspin(hw.client_timeout_s, 0, 600, 1, 1.0, "s"))
        self._add(form, "hardware", "emission_off_on_start", "At service start",
                  _check(hw.emission_off_on_start,
                         "switch emission off (default: adopt it)"))
        self._add(form, "hardware", "sim_warmup_s", "Sim warm-up",
                  _dspin(hw.sim_warmup_s, 0, 30, 1, 0.5, "s"))
        form.addRow(_hint("Used by the real backend (NKT SDK). The watchdog switches "
                          "emission off if the laser hears nothing for that long -- "
                          "it protects against a killed service. 0 disables it. "
                          "Lost-client guard: a remote GUI that switched emission "
                          "on and then falls silent this long -> emission off "
                          "(0 = never; scans are never guarded)."))
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
        try:
            self.ctrl.apply_config()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Settings", f"Not applied: {exc}")
            return
        self.on_applied()
        self.accept()

    def _save_config(self):
        self._pull_into_cfg()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "superk.ini",
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
