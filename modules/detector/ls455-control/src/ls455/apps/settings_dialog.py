"""Settings dialog: every config group, generated from the dataclasses.

Nothing here talks to hardware: it edits `cfg` in place and calls
`meter.apply_config()`, so a local meter and a remote service behave the same.

The forms are GENERATED from the dataclass fields -- one tab per group, a
checkbox for a bool, a drop-down for a fixed-choice
string (_CHOICES), a text box otherwise -- so adding a field to config.py
adds it here with no GUI edit. Numbers are text boxes parsed with float(),
because "3.5e-3" must work and QDoubleValidator follows the Windows locale
(suite gotcha #18).
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config, _cast
from ..backends.base import MODES, PROBE_GEOMETRIES, RMS_BANDS, UNIT_CODES

_TABS = [("meter", "Meter"), ("acquisition", "Acquisition"), ("limits", "Limits"),
         ("hardware", "Hardware"), ("ui", "Appearance")]

_HINTS = {
    "meter": "Pushed to the meter on Apply. mode: dc, rms or peak; dc_digits "
             "3/4/5; rms_band wide/narrow; display_unit G, T, Oe or A/m (front "
             "panel only -- readings here are always mT). At start-up nothing is "
             "pushed: the meter's own settings are read and shown here.",
    "acquisition": "An acquisition averages this many readings, all started at least "
                   "settle_time_constants filter time constants after the trigger.",
    "limits": "The envelope every setpoint is clamped to; the range is narrowed "
              "further by the ranges of the connected probe.",
    "hardware": "Used by the real pyvisa backend. resource: GPIB0::12::INSTR or "
                "ASRL<n>::INSTR (serial: 7 data bits, odd parity, fixed). "
                "Takes effect on the next start. probe_geometry: axial or "
                "transverse (the meter does not report it).",
    "ui": "theme: dark or light. Applies the next time the GUI starts.",
}


# Settings that only take one of a FIXED set of values get a drop-down instead
# of a text box, so a typo ("Light", "s21") cannot reach the config. The lists
# come from the code that checks the value (the same tuples where the code has
# one), so they cannot drift apart. (group, field) -> allowed values, spelled
# EXACTLY as the code expects them.
_CHOICES = {
    ("ui", "theme"): ["dark", "light"],
    ("meter", "mode"): list(MODES),
    ("meter", "rms_band"): list(RMS_BANDS),
    ("meter", "display_unit"): list(UNIT_CODES),        # G, T, Oe, A/m
    ("hardware", "probe_geometry"): list(PROBE_GEOMETRIES),
}


def _set_combo(box, choices, value) -> None:
    """Fill `box` with `choices` and select `value`.

    A value that is NOT in the list (an old or hand-edited .ini) is added,
    marked, rather than silently replaced by the first choice on Apply. Each
    item carries the real value as its data, so the mark never reaches cfg.
    blockSignals: a programmatic change is not a user edit (gotcha #13).
    """
    value = str(value)
    box.blockSignals(True)
    box.clear()
    for c in choices:
        box.addItem(c, c)
    if value not in choices:
        box.addItem(f"{value}  (not a known value)", value)
    box.setCurrentIndex(box.findData(value))
    box.blockSignals(False)


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, meter, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = meter
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self.w = {}                      # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        for group, title in _TABS:
            tabs.addTab(self._tab(group), title)

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
        self.error = QtWidgets.QLabel(""); self.error.setObjectName("hint")
        root.addWidget(self.error)
        root.addLayout(bar)

    def _tab(self, group: str):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setSpacing(8); form.setContentsMargins(16, 16, 16, 16)
        obj = getattr(self.cfg, group)
        for f in dataclass_fields(obj):
            val = getattr(obj, f.name)
            if f.type in ("bool", bool):
                w = QtWidgets.QCheckBox(); w.setChecked(bool(val))
            elif (group, f.name) in _CHOICES:
                w = QtWidgets.QComboBox()
                _set_combo(w, _CHOICES[(group, f.name)], val)
            else:
                w = QtWidgets.QLineEdit(f"{val:g}" if isinstance(val, float) else str(val))
            self.w[(group, f.name)] = (w, f.type)
            form.addRow(f.name.replace("_", " "), w)
        hint = QtWidgets.QLabel(_HINTS.get(group, "")); hint.setObjectName("hint")
        hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _pull_into_cfg(self) -> bool:
        """Parse every box first, write only if ALL parse -- a half-applied
        config is worse than none."""
        parsed = {}
        for (group, name), (w, typ) in self.w.items():
            try:
                if isinstance(w, QtWidgets.QCheckBox):
                    parsed[(group, name)] = w.isChecked()
                elif isinstance(w, QtWidgets.QComboBox):
                    parsed[(group, name)] = _cast(str(w.currentData()), typ)
                else:
                    parsed[(group, name)] = _cast(w.text().strip(), typ)
            except ValueError:
                self.error.setText(f"{group} / {name}: not a valid {typ}")
                return False
        for (group, name), v in parsed.items():
            setattr(getattr(self.cfg, group), name, v)
        return True

    def _refresh_widgets_from_cfg(self):
        for (group, name), (w, _typ) in self.w.items():
            val = getattr(getattr(self.cfg, group), name)
            if isinstance(w, QtWidgets.QCheckBox):
                w.setChecked(bool(val))
            elif isinstance(w, QtWidgets.QComboBox):
                _set_combo(w, _CHOICES[(group, name)], val)
            else:
                w.setText(f"{val:g}" if isinstance(val, float) else str(val))

    def _apply_and_close(self):
        if not self._pull_into_cfg():
            return
        self.ctrl.apply_config()
        self.on_applied()
        self.accept()

    def _save_config(self):
        if not self._pull_into_cfg():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "ls455.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            for group, _title in _TABS:
                d, s = getattr(self.cfg, group), getattr(loaded, group)
                for f in dataclass_fields(d):
                    setattr(d, f.name, getattr(s, f.name))
            self._refresh_widgets_from_cfg()
