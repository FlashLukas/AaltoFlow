"""Settings dialog: one tab per config group (§7 of the guide).

Edits a :class:`Config` in place.  Tabs mirror the dataclass groups (Motion,
Limits, Relative, Hardware) so the dialog stays in lock-step with ``config.py``
-- add a field there and add one row here automatically.
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
    QLineEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtWidgets import QLabel

from ..config import RAMP_MODES, Config

_GROUPS = ["motion", "limits", "relative", "hardware", "ui"]
_TITLES = {
    "motion": "Motion",
    "limits": "Limits",
    "relative": "Relative",
    "hardware": "Hardware",
    "ui": "Appearance",
}


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Piezo settings")
        self.resize(420, 460)
        self._editors: dict[tuple[str, str], object] = {}

        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        for group in _GROUPS:
            obj = getattr(cfg, group)
            page = QWidget()
            form = QFormLayout(page)
            for fld in fields(obj):
                editor = self._make_editor(fld.name, fld.type, getattr(obj, fld.name))
                self._editors[(group, fld.name)] = editor
                form.addRow(fld.name, editor)
            if group == "ui":
                hint = QLabel("Theme applies on the next launch.")
                hint.setObjectName("muted")
                hint.setWordWrap(True)
                form.addRow(hint)
            tabs.addTab(page, _TITLES[group])

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _make_editor(self, name: str, type_name: str, value):
        # ramp_mode is a small closed set -> a dropdown rather than free text.
        if name == "ramp_mode":
            w = QComboBox()
            w.addItems(list(RAMP_MODES))
            idx = w.findText(str(value))
            w.setCurrentIndex(idx if idx >= 0 else 0)
            return w
        # theme is dark/light -> a dropdown (startup-only; see hint on the tab).
        if name == "theme":
            w = QComboBox()
            w.addItems(["dark", "light"])
            idx = w.findText(str(value).lower())
            w.setCurrentIndex(idx if idx >= 0 else 0)
            return w
        if type_name == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            return w
        if type_name == "int":
            w = QSpinBox()
            w.setRange(-1_000_000, 1_000_000)
            w.setValue(int(value))
            return w
        if type_name == "float":
            w = QDoubleSpinBox()
            w.setRange(-1e9, 1e9)
            w.setDecimals(4)
            w.setValue(float(value))
            return w
        w = QLineEdit(str(value))
        return w

    def _accept(self) -> None:
        """Write the editors back into the Config groups in place."""
        for (group, name), editor in self._editors.items():
            obj = getattr(self.cfg, group)
            if isinstance(editor, QComboBox):
                setattr(obj, name, editor.currentText())
            elif isinstance(editor, QCheckBox):
                setattr(obj, name, editor.isChecked())
            elif isinstance(editor, QSpinBox):
                setattr(obj, name, editor.value())
            elif isinstance(editor, QDoubleSpinBox):
                setattr(obj, name, editor.value())
            else:  # QLineEdit
                setattr(obj, name, editor.text())
        self.accept()
