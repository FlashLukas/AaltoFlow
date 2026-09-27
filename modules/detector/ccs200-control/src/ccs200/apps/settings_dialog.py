"""Settings dialog: every config group, generated from the dataclasses.

Nothing here talks to hardware: it edits `cfg` in place and calls
`ccs200.apply_config()`, so a local spectrometer and a remote service behave
the same.

The forms are GENERATED from the dataclass fields -- one tab per group, a
checkbox for a bool, a text box otherwise -- so adding a field to config.py
adds it here with no GUI edit. Numbers are text boxes parsed with float(),
because "5e-8" must work and QDoubleValidator follows the Windows locale
(suite gotcha #18).
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config, _cast

_TABS = [("scan", "Scan"), ("analysis", "Analysis"), ("acquisition", "Acquisition"),
         ("sim", "Sim"), ("hardware", "Hardware"), ("limits", "Limits"),
         ("ui", "Appearance")]

_HINTS = {
    "scan": "Applied at the next scan. A change of integration time or averages during "
            "an acquisition restarts it. dark_subtract needs a dark taken at the SAME "
            "integration time, or acquire is refused.",
    "analysis": "The window (nm) the peak and integrated-intensity detectors look at. "
                "The spectrum itself is always the full range.",
    "acquisition": "timeout_s: the least a client waits for one acquisition; long "
                   "integrations x averages get more automatically.",
    "sim": "SIMULATOR only. Levels in full scale per second of integration. light_on off = "
           "the fibre is capped (take the dark like that). dark_rate grows with integration "
           "time; offset does not.",
    "hardware": "REAL CCS200 only (--real), read when it connects: restart the service after "
                "a change. resource: USB0::0x1313::0x8089::M<serial>::RAW, empty = first found. "
                "calibration: factory or user.",
    "limits": "The envelope every setpoint is clamped to.",
    "ui": "theme: dark or light. Applies the next time the GUI starts.",
}


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, ccs200, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = ccs200
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "ccs200.ini", "Config (*.ini)")
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
