"""Settings dialog: every config group, generated from the dataclasses.

Nothing here talks to hardware: it edits `cfg` in place and calls
`scope.apply_config()`, so a local scope and a remote service behave
the same.

The forms are GENERATED from the dataclass fields -- one tab per group, a
checkbox for a bool, a drop-down for a fixed-choice
string (_CHOICES), a text box otherwise -- so adding a field to config.py
adds it here with no GUI edit. Numbers are text boxes parsed with float(),
because "5e-8" must work and QDoubleValidator follows the Windows locale
(suite gotcha #18).
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config, _cast

_TABS = [("channel_1", "CH1"), ("channel_2", "CH2"), ("timebase", "Timebase"),
         ("trigger", "Trigger"), ("acquisition", "Acquisition"), ("filter", "Filter"),
         ("analysis", "Loop"), ("sim", "Sim"), ("hardware", "Hardware"),
         ("ui", "Appearance")]

_SCOPE_NOTE = ("The SCOPE's own settings: read from it at start; a value you CHANGE "
               "here is written to it on Apply (it may snap V/div and time/div to its "
               "steps). ")

_HINTS = {
    "channel_1": _SCOPE_NOTE + "phys_*: what the channel measures -- quantity = "
                 "phys_scale x volts + phys_offset, in phys_unit (module setting).",
    "channel_2": _SCOPE_NOTE + "phys_*: as for CH1.",
    "timebase": _SCOPE_NOTE + "delay_s: positive shows more time before the trigger.",
    "trigger": _SCOPE_NOTE + "A stopped scope makes no traces; an acquisition is refused.",
    "acquisition": "points: samples per recorded trace (must not change during a scan). "
                   "averages: fresh traces per acquisition. timeout_s / min_trigger_hz set "
                   "how long an acquisition may take. keep_raw: record unfiltered too.",
    "filter": "Zero phase, the same on every channel. 0 = off. order: of one pass "
              "(the zero-phase response is its square).",
    "analysis": "loop_x / loop_y: channels of the loop. sat_fraction: |X| above this "
                "fraction of the maximum counts as saturated (levels and background "
                "are fitted there).",
    "sim": "SIMULATOR only. scene moke: CH1 Hall field, CH2 a hysteresis loop; scene "
           "bench: CH1 sine, CH2 + EXT a synchronous square (the lab bench).",
    "hardware": "REAL scope only (--real), read when the service starts: restart it "
                "after a change. visa: usually passed by Mission Control (--visa).",
    "ui": "theme: dark or light. Applies the next time the GUI starts.",
}


# Settings that only take one of a FIXED set of values get a drop-down instead
# of a text box, so a typo cannot reach the config. The lists come from the
# code that checks the value, so they cannot drift apart.
from ..config import COUPLINGS, TRIGGER_SOURCES, TRIGGER_SLOPES, TRIGGER_MODES  # noqa: E402

_CHOICES = {
    ("ui", "theme"): ["dark", "light"],
    ("channel_1", "coupling"): list(COUPLINGS),
    ("channel_2", "coupling"): list(COUPLINGS),
    ("trigger", "source"): list(TRIGGER_SOURCES),
    ("trigger", "slope"): list(TRIGGER_SLOPES),
    ("trigger", "mode"): list(TRIGGER_MODES),
    ("analysis", "loop_x"): ["ch1", "ch2"],
    ("analysis", "loop_y"): ["ch1", "ch2"],
    ("sim", "scene"): ["moke", "bench"],
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
    def __init__(self, scope, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = scope
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
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "scope.ini", "Config (*.ini)")
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
