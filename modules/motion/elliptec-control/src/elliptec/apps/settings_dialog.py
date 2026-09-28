"""Settings dialog: one tab per config group (section 7 of the guide).

Edits a :class:`Config` in place.  The tabs are generated from the dataclass
groups in ``config.py``, so a new field appears here without editing this file.
A changed address list takes effect only after the service is restarted (the
serial bus was opened with the old one) -- the Axes tab says so.
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

from PySide6.QtCore import QLocale

from ..config import GROUPS, Config

_GROUPS = [g for g in GROUPS if g != "ui"]   # ui has its own Appearance tab
_TITLES = {g: g.capitalize() for g in _GROUPS}
_HINTS = {
    "axes": "Comma-separated bus addresses (0-F), one mount each, and optional "
            "names in the same order. Takes effect after a restart of the service.",
    "offsets": "Per-axis zero in degrees, comma-separated (user = device - offset).",
    "sim": "Simulator only; ignored with --real.",
}


# Qt number widgets follow the Windows locale unless told otherwise (gotcha #18).
_C = QLocale.c()
_C.setNumberOptions(QLocale.OmitGroupSeparator)


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Elliptec settings")
        self.resize(420, 480)
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
            if group in _HINTS:
                hint = QLabel(_HINTS[group])
                hint.setObjectName("hint")
                hint.setWordWrap(True)
                form.addRow(hint)
            tabs.addTab(page, _TITLES[group])

        tabs.addTab(self._appearance_tab(), "Appearance")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _appearance_tab(self) -> QWidget:
        """A dedicated tab for the light/dark theme (applied at next launch)."""
        page = QWidget()
        form = QFormLayout(page)
        combo = QComboBox()
        combo.addItems(["dark", "light"])
        combo.setCurrentText(getattr(self.cfg.ui, "theme", "dark"))
        self._editors[("ui", "theme")] = combo
        form.addRow("theme", combo)
        hint = QLabel(
            "Applies on the next launch (startup-only, no live toggle). "
            "Use an INI to persist it, or override once with  run_gui.py --theme light."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _make_editor(self, type_name: str, value):
        if type_name == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            return w
        if type_name == "int":
            w = QSpinBox()
            w.setLocale(_C)
            w.setRange(-1_000_000, 1_000_000)
            w.setValue(int(value))
            return w
        if type_name == "float":
            w = QDoubleSpinBox()
            w.setLocale(_C)
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
            elif isinstance(editor, QComboBox):
                setattr(obj, name, editor.currentText())
            elif isinstance(editor, QSpinBox):
                setattr(obj, name, editor.value())
            elif isinstance(editor, QDoubleSpinBox):
                setattr(obj, name, editor.value())
            else:  # QLineEdit
                setattr(obj, name, editor.text())
        self.accept()
