"""Settings dialog: every config group as a tab, plus load/save.

Like every module's Settings pane it never talks to hardware: it edits the
shared `cfg` IN PLACE and calls `lockin.apply_config()`, which re-validates and
writes to the SR830 only the settings that differ from what it is set to
(locally, or over the socket for a remote client).

The forms are GENERATED from the dataclasses rather than written by hand, so a
field added to config.py appears here without another edit. Floats use a text
box, not a spin box: a frequency of 0.001 Hz and a limit of 102e3 Hz do not fit
any one spin box's decimals, and typing "1e-3" is what a physicist does anyway.
Enum fields (config.CHOICES) become drop-downs.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config, CHOICES
from .theme import COLORS


_TAB_TITLES = {
    "reference": "Reference", "input": "Input", "demod": "Gain / filter",
    "aux_out": "Aux out", "acquisition": "Acquisition", "limits": "Limits",
    "safety": "Safety", "hardware": "Hardware", "ui": "Appearance",
}

_HINTS = {
    "reference": "internal: the SR830's own oscillator at frequency_Hz. external: "
                 "it locks to REF IN (trigger = how REF IN is read). sine_out_V is "
                 "the SINE OUT amplitude in Vrms; it cannot be switched off (4 mV "
                 "minimum).",
    "input": "A, A-B, or current input I1M / I100M (then X, Y, R are in amps). "
             "Line notch = the 50 Hz (and 100 Hz) filters.",
    "demod": "Sensitivity and time constant are the SR830's fixed steps. The "
             "instrument may change the time constant on its own (above 200 Hz, "
             "or with reserve / slope); the panel shows what it applied.",
    "aux_out": "The four rear-panel AUX OUT voltages. At start the service "
               "ADOPTS what the SR830 outputs; a value here is written only "
               "when you change it and Apply.",
    "acquisition": "`acquire` waits until a step has reached settle_percent at the "
                   "filter output (computed from time constant and slope), then "
                   "averages over average_tc time constants. Raise timeout_s for "
                   "very long time constants.",
    "limits": "Every setpoint is clamped to this envelope before it reaches the "
              "instrument. Narrow sine / aux out to protect what is connected.",
    "safety": "What the service does to the OUTPUTS when it stops cleanly. A hard "
              "kill can not do this.",
    "hardware": "Used by the real backend only. The SR830 ships at GPIB address 8 "
                "(front panel [Setup] key). front_panel_override keeps the knobs "
                "usable while under remote control.",
    "ui": "Light or dark colour scheme. A start-up setting: it applies the next "
          "time the GUI is launched.",
}


def _copy_config_into(dst: Config, src: Config) -> None:
    """Copy every field from src into dst's existing sub-objects IN PLACE."""
    for group in Config._GROUPS:
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


def _raw(w):
    """A widget's value as shown (text for number fields: compared, not parsed)."""
    if isinstance(w, QtWidgets.QCheckBox):
        return bool(w.isChecked())
    if isinstance(w, QtWidgets.QComboBox):
        return w.currentText()
    if isinstance(w, QtWidgets.QSpinBox):
        return int(w.value())
    return w.text()


class SettingsPanel(QtWidgets.QWidget):
    """Every config group as a tab of generated form fields.

    A plain widget, so the main window's Instrument tab can hold it; the
    SettingsDialog below wraps the same panel for anyone who wants a dialog.
    """

    def __init__(self, lockin, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = lockin
        self.cfg = cfg
        self.on_applied = on_applied
        self.w = {}          # (group, field) -> (widget, type name)

        tabs = QtWidgets.QTabWidget()
        for group in Config._GROUPS:
            tabs.addTab(self._scroll(self._tab(group)), _TAB_TITLES.get(group, group))

        self.bar = QtWidgets.QHBoxLayout()
        load_btn = QtWidgets.QPushButton("Load config..."); load_btn.clicked.connect(self._load_config)
        save_btn = QtWidgets.QPushButton("Save config..."); save_btn.clicked.connect(self._save_config)
        revert = QtWidgets.QPushButton("Revert")
        revert.setToolTip("Throw away edits here and show the settings in use")
        revert.clicked.connect(self.reload)
        self.bar.addWidget(load_btn); self.bar.addWidget(save_btn); self.bar.addStretch(1)
        self.bar.addWidget(revert)

        self.error = QtWidgets.QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{COLORS['danger']}; font-weight:600;")

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(tabs, 1)
        root.addWidget(self.error)
        self._remember_shown()
        root.addLayout(self.bar)

    @staticmethod
    def _scroll(page):
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.NoFrame)
        area.setWidget(page)
        return area

    def add_apply_button(self, text="Apply", slot=None):
        btn = QtWidgets.QPushButton(text); btn.setObjectName("primary")
        btn.clicked.connect(slot or self.apply)
        self.bar.addWidget(btn)
        return btn

    def apply(self) -> bool:
        """Apply the fields the user EDITED -- and only those.

        Why not copy every field into cfg: the form was filled when the tab was
        opened. Since then a front-panel control, the console or a scan may have
        changed, say, the sensitivity; copying the whole (stale) form would
        quietly set it back. So: note which widgets differ from what they were
        filled with, fetch the settings in use NOW (get_config: the brain's cfg,
        or the service's over the socket), put only the edited fields on top,
        and apply. The brain then writes to the SR830 only what differs.
        """
        edited = {key for key, (w, _t) in self.w.items()
                  if _raw(w) != self._shown.get(key)}
        staged = self._parse_widgets(edited)
        if staged is None:
            return False
        try:
            self.ctrl.get_config()              # the settings in use right now
            for (group, name), v in staged.items():
                setattr(getattr(self.cfg, group), name, v)
            self.ctrl.apply_config()
        except ValueError as exc:
            self.error.setText(str(exc))
            return False
        self.reload()                           # show what was really applied
        self.on_applied()
        return True

    def reload(self):
        """Show the settings actually in use (from the service when remote)."""
        self.ctrl.get_config()
        self._refresh_widgets_from_cfg()
        self.error.setText("")

    # ---- form generation ---------------------------------------------------------

    def _tab(self, group: str):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setSpacing(8); form.setContentsMargins(16, 16, 16, 16)
        obj = getattr(self.cfg, group)
        for f in dataclass_fields(obj):
            widget = self._widget_for(group, f.name, f.type, getattr(obj, f.name))
            self.w[(group, f.name)] = (widget, f.type)
            form.addRow(f.name.replace("_", " "), widget)
        hint = QtWidgets.QLabel(_HINTS.get(group, ""))
        hint.setObjectName("hint"); hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _widget_for(self, group, name, type_name, value):
        if type_name in ("bool", bool):
            w = QtWidgets.QCheckBox(); w.setChecked(bool(value))
        elif (group, name) in CHOICES:
            # an enum: a drop-down, so a typo cannot reach the instrument
            w = QtWidgets.QComboBox(); w.addItems(list(CHOICES[(group, name)]))
            w.setCurrentText(str(value))
        elif type_name in ("int", int):
            w = QtWidgets.QSpinBox(); w.setRange(-1_000_000, 1_000_000_000); w.setValue(int(value))
        elif type_name in ("float", float):
            # No QDoubleValidator: it follows the Windows LOCALE, and with a
            # comma-decimal locale it would reject "0.01". float() at Apply
            # time checks the text and says which field is wrong.
            w = QtWidgets.QLineEdit(f"{float(value):.10g}")
        else:
            w = QtWidgets.QLineEdit(str(value))
        return w

    # ---- read widgets back into cfg ----------------------------------------------

    def _pull_into_cfg(self) -> bool:
        """Copy every widget into cfg. Returns False (and says why) on bad input."""
        staged = self._parse_widgets(set(self.w))
        if staged is None:
            return False
        for (group, name), v in staged.items():
            setattr(getattr(self.cfg, group), name, v)
        self.error.setText("")
        return True

    def _parse_widgets(self, keys) -> dict | None:
        """{(group, name): typed value} for the widgets in `keys`; None (and the
        reason in the error label) if a number field does not parse."""
        staged = {}
        for (group, name), (w, type_name) in self.w.items():
            if (group, name) not in keys:
                continue
            if isinstance(w, QtWidgets.QCheckBox):
                v = bool(w.isChecked())
            elif isinstance(w, QtWidgets.QComboBox):
                v = w.currentText()
            elif isinstance(w, QtWidgets.QSpinBox):
                v = int(w.value())
            elif type_name in ("float", float):
                try:
                    v = float(w.text())
                except ValueError:
                    self.error.setText(f"{_TAB_TITLES.get(group, group)}: '{name}' "
                                       f"is not a number")
                    return None
            else:
                v = w.text().strip()
            staged[(group, name)] = v
        self.error.setText("")
        return staged

    def _refresh_widgets_from_cfg(self):
        for (group, name), (w, type_name) in self.w.items():
            val = getattr(getattr(self.cfg, group), name)
            if isinstance(w, QtWidgets.QCheckBox):
                w.setChecked(bool(val))
            elif isinstance(w, QtWidgets.QComboBox):
                w.setCurrentText(str(val))
            elif isinstance(w, QtWidgets.QSpinBox):
                w.setValue(int(val))
            elif type_name in ("float", float):
                w.setText(f"{float(val):.10g}")
            else:
                w.setText(str(val))
        self._remember_shown()

    def _remember_shown(self):
        """What each widget was filled with, so apply() can tell an edit."""
        self._shown = {key: _raw(w) for key, (w, _t) in self.w.items()}

    # ---- actions -------------------------------------------------------------------

    def _save_config(self):
        if not self._pull_into_cfg():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "sr830.ini",
                                                        "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            _copy_config_into(self.cfg, Config.load(path))
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()


class SettingsDialog(QtWidgets.QDialog):
    """The same SettingsPanel in a dialog, with Cancel and Apply-and-close."""

    def __init__(self, lockin, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self.panel = SettingsPanel(lockin, cfg, on_applied, self)
        cancel = QtWidgets.QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        self.panel.bar.addWidget(cancel)
        self.panel.add_apply_button("Apply", self._apply_and_close)
        QtWidgets.QVBoxLayout(self).addWidget(self.panel)

    @property
    def w(self):
        return self.panel.w

    @property
    def error(self):
        return self.panel.error

    def _apply_and_close(self):
        if self.panel.apply():
            self.accept()
