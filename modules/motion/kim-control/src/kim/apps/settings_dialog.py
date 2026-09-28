"""Settings dialog: one tab per config group (§7 of the guide).

Edits a :class:`Config` in place.  Tabs mirror the dataclass groups (Motion,
Calibration, Limits, Relative, Hardware) so the dialog stays in lock-step with
``config.py`` -- add a field there and it appears here automatically.
"""

from __future__ import annotations

from dataclasses import fields

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import Config

_GROUPS = ["motion", "calibration", "limits", "relative", "hardware"]
_TITLES = {
    "motion": "Motion",
    "calibration": "Calibration",
    "limits": "Limits",
    "relative": "Relative",
    "hardware": "Hardware",
}


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("KIM101 stage settings")
        self.resize(440, 520)
        self._editors: dict[tuple[str, str], object] = {}

        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        for group in _GROUPS:
            obj = getattr(cfg, group)
            page = QWidget()
            form = QFormLayout(page)
            for fld in fields(obj):
                editor = self._make_editor(fld.type, getattr(obj, fld.name))
                self._editors[(group, fld.name)] = editor
                form.addRow(fld.name, editor)
            tabs.addTab(page, _TITLES[group])

        # -- Appearance tab (theme; applies on next launch) --------------- #
        appearance = QWidget()
        aform = QFormLayout(appearance)
        self._theme_combo = QComboBox()
        self._theme_combo.addItems(["dark", "light"])
        current = (getattr(cfg.ui, "theme", "dark") or "dark").lower()
        self._theme_combo.setCurrentText(current if current in ("dark", "light") else "dark")
        aform.addRow("theme", self._theme_combo)
        hint = QLabel("Applies on next launch (startup-only).")
        hint.setObjectName("hint")
        aform.addRow("", hint)
        tabs.addTab(appearance, "Appearance")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _make_editor(self, type_name: str, value):
        if type_name == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            return w
        if type_name == "int":
            w = QSpinBox()
            w.setRange(-10_000_000, 10_000_000)
            w.setValue(int(value))
            return w
        if type_name == "float":
            w = QDoubleSpinBox()
            w.setRange(-1e9, 1e9)
            w.setDecimals(5)
            w.setValue(float(value))
            return w
        w = QLineEdit(str(value))
        return w

    def _accept(self) -> None:
        """Write the editors back into the Config groups in place."""
        for (group, name), editor in self._editors.items():
            obj = getattr(self.cfg, group)
            if isinstance(editor, QCheckBox):
                setattr(obj, name, editor.isChecked())
            elif isinstance(editor, QSpinBox):
                setattr(obj, name, editor.value())
            elif isinstance(editor, QDoubleSpinBox):
                setattr(obj, name, editor.value())
            else:  # QLineEdit
                setattr(obj, name, editor.text())
        # Appearance tab -> cfg.ui.theme (takes effect on next launch)
        self.cfg.ui.theme = self._theme_combo.currentText()
        self.accept()
