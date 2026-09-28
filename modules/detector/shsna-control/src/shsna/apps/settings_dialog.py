"""Settings dialog: every config group, generated from the dataclasses.

Nothing here talks to hardware: it edits `cfg` in place and calls
`shsna.apply_config()`, so a local analyser and a remote service behave the same.

The forms are GENERATED from the dataclass fields -- one tab per group, a
checkbox for a bool, a drop-down for a text setting with a FIXED set of valid
values (`_CHOICES`), a text box otherwise -- so adding a field to config.py
adds it here with no GUI edit. (A free text box for "dark"/"light" invited a
typo that silently fell back to the default: Lukas, 2026-09-28.) Numbers are text boxes parsed with float(),
because "5e-8" must work and QDoubleValidator follows the Windows locale
(suite gotcha #18).
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..analyzer import SIM_CHOICES
from ..config import Config, _cast
from ..field import FIELD_SOURCES

_TABS = [("sweep", "Sweep"), ("acquisition", "Acquisition"), ("sim", "Simulation"),
         ("field", "Sim field"), ("hardware", "Analyser service"), ("limits", "Limits"),
         ("ui", "Appearance")]

#: text settings with a fixed set of valid values -> a QComboBox. The lists
#: come from where the values are checked, so the two cannot drift apart.
_CHOICES = {
    ("ui", "theme"): ("dark", "light"),
    ("sim", "fmr_geometry"): SIM_CHOICES["fmr_geometry"],
    ("field", "source"): FIELD_SOURCES,
}

_HINTS = {
    "sweep": "Applied at the next TG sweep. A change during an acquisition restarts it. "
             "points: asked for, the analyser has the last word. rbw 0 = the analyser's "
             "default. Averaging is in linear power. (No TG level: the TG44A ignores it "
             "in sweep mode.)",
    "acquisition": "An acquisition is one TG sweep block that started after the trigger. "
                   "continuous = sweep on its own between acquisitions (each sweep pauses "
                   "the analyser's spectrum display and any signal-generator output).",
    "sim": "SIMULATOR only. The chain TG -> cable -> pad -> DUT -> analyser; "
           "dut_inserted off = the thru a reference is taken with. fmr on: the DUT is a "
           "waveguide with a magnetic film (Meff, g, in-plane uniaxial Hk along the easy "
           "axis, geometry inplane/outofplane) that absorbs a Lorentzian dip of depth dB "
           "at its Kittel frequency; linewidth from alpha, or fixed when linewidth Hz > 0.",
    "field": "SIMULATOR only, used while the film is on: where its field comes from. "
             "A magnet service's status stream (mag2d / mag2dcal: |B| and angle from Bx, By; "
             "clMag / ppms: signed field, angle 0), or manual. Only listens; never "
             "commands a magnet. A magnet not heard falls back to the manual value.",
    "hardware": "REAL only (--real): where the signalhound service listens, which owns "
                "the analyser and its TG. Restart this service after a change.",
    "limits": "The envelope every setpoint is clamped to (TG44A: 10 Hz - 4.4 GHz, "
              "at most 1001 points).",
    "ui": "theme: dark or light. Applies the next time the GUI starts.",
}


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, shsna, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = shsna
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
                w.addItems(list(_CHOICES[(group, f.name)]))
                _set_combo(w, val)
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
                    parsed[(group, name)] = w.currentText()
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
                _set_combo(w, val)
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "shsna.ini", "Config (*.ini)")
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


def _set_combo(w, value) -> None:
    """Select `value` in a drop-down. A value that is not in the list (an old
    or hand-edited .ini) is ADDED rather than silently replaced by the first
    entry: the dialog must not change a setting the user did not touch."""
    text = str(value)
    if w.findText(text) < 0:
        w.addItem(text)
    w.setCurrentText(text)
