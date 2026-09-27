"""Settings dialog: edit every tunable, plus load/save config.

Nothing here talks to hardware: it edits the shared `cfg` object in place and
then calls `apply_config()` so the running brain (local, or remote over the
socket) picks the changes up. Nothing is MOVED by applying settings.

The form is built from the config dataclasses themselves (one row per field,
the widget chosen by the field's type), so a new config field shows up here
without anyone editing this file -- only a nicer label is optional.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config

#: group -> (tab title, hint under the form)
_TABS = {
    "gratings": ("Gratings", "What is on the turret. lines/mm and labels stored in the "
                             "instrument win for display; the ranges are the safety "
                             "envelope a wavelength is clamped to. min 0 allows zero order."),
    "accessories": ("Accessories", "Only tick what is fitted: an absent accessory is "
                                   "left out of describe and refused. Filter bands: "
                                   "'position:from-to' in nm, comma separated."),
    "shutter": ("Shutter", "Closing on start is optional. The shutter is closed during a "
                           "grating change because the drive sweeps past zero order."),
    "motion": ("Motion", "How moves are sequenced and judged."),
    "optics": ("Optics", "Only used to report the bandpass: dispersion x slit width."),
    "limits": ("Limits", "Absolute wavelength envelope, intersected with each grating's range."),
    "hardware": ("Hardware", "Used by the real GPIB backend only. The Cornerstone's "
                             "factory GPIB address is 4."),
    "sim": ("Simulator", "The simulator's physics. Ignored by the real backend."),
}

_LABELS = {
    "count": "Gratings fitted",
    "filter_wheel": "Filter wheel fitted", "dual_port": "Two exit ports",
    "auto_filter": "Automatic order sorting",
    "visa": "VISA address", "timeout_ms": "Query timeout (ms)",
    "move_timeout_ms": "Query timeout during a move (ms)",
}


def _copy_config_into(dst: Config, src: Config) -> None:
    """Copy every field from src into dst's existing sub-objects IN PLACE, so
    shared references stay valid."""
    for group in Config._GROUPS:
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


def _widget_for(value):
    if isinstance(value, bool):
        w = QtWidgets.QCheckBox(); w.setChecked(value)
    elif isinstance(value, int):
        w = QtWidgets.QSpinBox(); w.setRange(-1_000_000, 1_000_000); w.setValue(value)
    elif isinstance(value, float):
        w = QtWidgets.QDoubleSpinBox()
        w.setRange(-1e7, 1e7); w.setDecimals(3); w.setValue(value)
    else:
        w = QtWidgets.QLineEdit(str(value))
    return w


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, mono, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = mono
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        for group, (title, hint) in _TABS.items():
            tabs.addTab(self._group_tab(group, hint), title)
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

    def _group_tab(self, group: str, hint: str):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setSpacing(8); form.setContentsMargins(16, 16, 16, 16)
        obj = getattr(self.cfg, group)
        for f in dataclass_fields(obj):
            w = _widget_for(getattr(obj, f.name))
            self.w[(group, f.name)] = w
            form.addRow(_LABELS.get(f.name, f.name.replace("_", " ")), w)
        lbl = QtWidgets.QLabel(hint); lbl.setObjectName("hint"); lbl.setWordWrap(True)
        form.addRow(lbl)
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _appearance_tab(self):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setContentsMargins(16, 16, 16, 16)
        combo = QtWidgets.QComboBox()
        combo.addItems(["dark", "light"])
        combo.setCurrentText(getattr(self.cfg.ui, "theme", "dark"))
        self.w[("ui", "theme")] = combo
        form.addRow("Theme", combo)
        lbl = QtWidgets.QLabel("Light or dark colour scheme. A start-up setting: it takes "
                               "effect the next time you launch the GUI.")
        lbl.setObjectName("hint"); lbl.setWordWrap(True)
        form.addRow(lbl)
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

    def _apply_and_close(self):
        self._pull_into_cfg()
        self.ctrl.apply_config()
        self.on_applied()
        self.accept()

    def _save_config(self):
        self._pull_into_cfg()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "cs260.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()
