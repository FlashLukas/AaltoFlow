"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `supply.apply_config()` so the running supply (local or
remote) picks the changes up (re-clamped to the limits).

The form is BUILT FROM THE CONFIG DATACLASSES: one tab per group, one widget per
field, chosen by the field's type. A new config field therefore appears here
without an edit (gotcha #4 lists the places a new group must go; this is no
longer one of them). Only the labels and hints below are hand-written.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config, MODES

#: tab title, hint -- per config group, in tab order
_TABS = {
    "output": ("Output", "The live operating point (the service keeps these in "
                         "step with what you set). At start they are READ "
                         "from the BOP, never pushed to it."),
    "ramp": ("Ramp", "Setpoints are walked at this rate. Keep the ramp ON with "
                     "any inductive load: V = L dI/dt."),
    "limits": ("Limits", "Every setpoint is clamped to this envelope. The BOP "
                         "20-10 is rated +-20 V / +-10 A; narrow it to protect "
                         "the load."),
    "safety": ("Safety", "shutdown_ramp_s: the ramp to zero on shutdown is "
                         "sped up to finish within this time (keep < 8 s). "
                         "watchdog_s > 0: ramp down if no client has spoken "
                         "for that long (the GUI pings; scan-core does not)."),
    "acquisition": ("Acquisition", "acquire() waits settle_s (the BOP's "
                                   "readback averages its last 16 readings, "
                                   "~320 ms), then averages `readings` "
                                   "fresh measurements."),
    "hardware": ("Hardware", "Used by the real GPIB backend. 6 is the BIT 4886 "
                             "factory address. full_range pins VOLT:RANG 1 so "
                             "no transient at 1/4 scale."),
    "sim": ("Simulator", "The simulated load (a coil: R in series with L). "
                         "Ignored by the real backend."),
    "ui": ("Appearance", "Light or dark colour scheme. A start-up setting: it "
                         "takes effect the next time you launch the GUI."),
}

_CHOICES = {("output", "mode"): list(MODES), ("ui", "theme"): ["dark", "light"]}


def _hint(text):
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("hint"); lbl.setWordWrap(True)
    return lbl


def _copy_config_into(dst: Config, src: Config) -> None:
    """Copy every field from src into dst's existing sub-objects IN PLACE, so
    shared references stay valid."""
    for group in Config._GROUPS:
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, supply, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = supply
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        for group, (title, hint) in _TABS.items():
            tabs.addTab(self._group_tab(group, hint), title)

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

    # ---- one tab per config group ------------------------------------------

    def _group_tab(self, group: str, hint: str):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setSpacing(8); form.setContentsMargins(16, 16, 16, 16)
        obj = getattr(self.cfg, group)
        for f in dataclass_fields(obj):
            val = getattr(obj, f.name)
            if (group, f.name) in _CHOICES:
                w = QtWidgets.QComboBox(); w.addItems(_CHOICES[(group, f.name)])
                w.setCurrentText(str(val))
            elif isinstance(val, bool):
                w = QtWidgets.QCheckBox(); w.setChecked(val)
            elif isinstance(val, int):
                w = QtWidgets.QSpinBox(); w.setRange(-1_000_000, 1_000_000); w.setValue(val)
            elif isinstance(val, float):
                w = QtWidgets.QDoubleSpinBox(); w.setRange(-1e6, 1e6)
                w.setDecimals(4); w.setValue(val)
            else:
                w = QtWidgets.QLineEdit(str(val))
            self.w[(group, f.name)] = w
            form.addRow(f.name, w)
        form.addRow(_hint(hint))
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
            else:
                widget.setValue(val)

    # ---- actions ---------------------------------------------------------

    def _apply(self):
        self._pull_into_cfg()
        try:
            self.ctrl.apply_config()
        except ValueError as exc:          # a remote refusal comes back as ValueError
            QtWidgets.QMessageBox.warning(self, "Settings", str(exc))
        self.on_applied()

    def _apply_and_close(self):
        self._apply()
        self.accept()

    def _save_config(self):
        self._pull_into_cfg()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "kepco.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self._apply()
