"""Settings dialog: every config group, generated from the dataclasses.

Nothing here talks to hardware: it edits `cfg` in place and calls
`ctrl.apply_config()`, so a local Controller and a remote service behave the
same (the client pushes the whole config with set_config).

The forms are GENERATED from the dataclass fields -- one tab per group, a
checkbox for a bool, a text box otherwise -- so adding a field to config.py
adds it here with no GUI edit. Numbers are text boxes parsed with float(),
because "5e-3" must work and Qt's number validators follow the Windows locale
(suite gotcha #18).
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config, _cast

_TABS = [("control", "Control"), ("limits", "Limits"), ("interlock", "Interlock"),
         ("hall", "Hall probes"), ("temperature", "Temperature"),
         ("hardware", "Hardware"), ("sim", "Simulator"), ("ui", "Appearance")]

_HINTS = {
    "control": "PI per axis in mT. Gains and ff_mT_per_V are SIM-TUNED: retune them on "
               "the real magnet (measure mT per volt first). Apply takes effect at once.",
    "limits": "Setpoints outside the envelope are clamped with a warning. field_max_mT "
              "is |B|; the Hall probes read about +-186 mT.",
    "interlock": "water_bypass lets the coils run without the flow switch -- DANGER. "
                 "temp_monitor faults when a temperature exceeds max_temp_C.",
    "hall": "B = (V - offset) / (k / 1000), per axis. From the old LabVIEW VI.",
    "temperature": "T = C_per_V * V + offset. The VI used 10e3 C/V -- VERIFY on the hardware.",
    "hardware": "NI DAQ channel names (as in NI MAX). Used by the real backend only; "
                "takes effect on the next service start.",
    "sim": "The simulated magnet. water_ok = False here trips the water interlock, "
           "which is a safe way to see a FAULT.",
    "ui": "theme: dark or light. Applies the next time the GUI starts.",
}


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, ctrl, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self.w = {}                      # (group, field) -> (widget, type name)

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
        inner = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(inner)
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
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(inner); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        return scroll

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
        try:
            self.ctrl.apply_config()
        except Exception as exc:          # a remote refusal must not lose the dialog
            self.error.setText(f"not applied: {exc}")
            return
        self.on_applied()
        self.accept()

    def _save_config(self):
        if not self._pull_into_cfg():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "mag2d.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            # copy IN PLACE: the simulator and the controller hold these objects
            for group, _title in _TABS:
                d, s = getattr(self.cfg, group), getattr(loaded, group)
                for f in dataclass_fields(d):
                    setattr(d, f.name, getattr(s, f.name))
            self._refresh_widgets_from_cfg()
