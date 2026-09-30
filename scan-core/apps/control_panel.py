"""control_panel.py -- the Raw control tab, built entirely from `describe`.

This is the replacement for the old LabVIEW VI's "Raw control" tab, and the
reason every module was taught to describe itself. Nothing here knows what a
magnet or a piezo stage is. It asks each connected service what knobs it has,
and builds the widgets from the answer:

    float / int  ->  spin box + Set, clamped to the module's LIVE limits
    bool         ->  checkbox
    enum         ->  combo of the module's own options
    string       ->  read-only field
    action       ->  button, with a confirmation when `danger` is set
    indicator    ->  readout, optionally a live strip chart

Two things this leans on, both deliberate:

* It reads the manifest DIRECTLY rather than going through scan-core's registry.
  The registry is the projection of a manifest for SCANNING -- it drops actions
  (a button is not a value you sweep) and demotes enums (a Settable is numeric).
  A control panel wants exactly those things back, plus the `group`/`order`
  layout hints. Same single source of truth, a different view of it.

* Limits move. piezo's ceiling drops 200 -> 160 um on closed loop, kim's armed
  leash replaces the travel clamp, clMag's field range IS the calibration. Every
  status frame carries `describe_rev`, so the panel compares one integer per
  poll and re-reads the manifest only when it actually changed -- then re-clamps
  its spin boxes. A panel offering travel the stage no longer has is how an
  operator ends up wondering why a move "did nothing".
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from suite_common.control import describe_holder, same_pc

from apps.theme import C

#: Where the chosen panel layouts live. Next to the project, like
#: mission-control's profiles.json, and gitignored for the same reason: it is
#: this machine's preference, not part of the software.
LAYOUTS_PATH = Path(__file__).resolve().parent.parent / "suite_layouts.json"

#: How many points a strip chart keeps. At a 4 Hz poll this is ~4 minutes,
#: which is long enough to see a magnet settle and short enough to stay cheap.
HISTORY = 1000

#: Where a tree row keeps its kind (control / indicator / action). Qt lets a
#: row carry extra data under numbered "roles"; UserRole already holds the pid.
KIND_ROLE = QtCore.Qt.UserRole + 1


# --------------------------------------------------------------------------- #
# Turning whatever we are connected to into a flat list of panel items
# --------------------------------------------------------------------------- #

def items_from_lab(lab, prefix: bool = True) -> list[dict]:
    """Panel items from the live manifests of every connected instrument."""
    items = []
    for name, inst in getattr(lab, "instruments", {}).items():
        manifest = getattr(inst, "manifest", None)
        if not manifest:
            continue
        module = getattr(inst, "alias", None) or manifest.get("module", name)
        for d in manifest.get("parameters", []):
            item = dict(d)
            item["module"] = module
            item["pid"] = f"{module}.{d['id']}" if prefix else d["id"]
            item["_inst"] = inst
            items.append(item)
    return items


def items_from_registry(registry) -> list[dict]:
    """Panel items synthesised from a registry that has no manifests.

    The simulated registry is plain Parameters -- no groups, no settle rules.
    Rather than blank the tab when there is no hardware, describe what we do
    have, so the panel is developable and demoable with nothing plugged in. It
    is a genuinely reduced view, not a pretend one: no buttons appear. (The sim
    registry does carry one scan Action, `vna_reference`, since 2026-09-16 --
    it is for routines on the Scan tab and is deliberately not drawn here.)
    """
    items = []
    for p in registry.settables():
        items.append({
            "id": p.id, "pid": p.id, "module": "sim", "label": p.label,
            "kind": "control", "type": "float", "unit": p.unit,
            "group": "Simulated", "order": 0,
            "min": p.limits[0], "max": p.limits[1],
            "_param": p,
        })
    for p in registry.gettables():
        items.append({
            "id": p.id, "pid": p.id, "module": "sim", "label": p.label,
            "kind": "indicator",
            "type": "array" if getattr(p, "is_array", False) else "float",
            "unit": p.unit, "group": "Simulated readouts", "order": 0,
            "plottable": not getattr(p, "is_array", False),
            "_param": p,
        })
    return items


# --------------------------------------------------------------------------- #
# What KIND an entry is: a small marker in the "tick to show" tree
# --------------------------------------------------------------------------- #

#: One line per kind: (legend word, tooltip sentence). The manifest's `kind` is
#: one of these three; anything else is drawn as an indicator, which is the safe
#: reading ("you can look at it") of an entry we do not understand.
KIND_TEXT = {
    "control": ("set", "Control -- a value you can SET (and read back)."),
    "indicator": ("read", "Indicator -- READ-ONLY, the module reports it."),
    "action": ("run", "Action -- a button that RUNS something on the module."),
}


def kind_of(item: dict) -> str:
    """The item's kind, normalised to one of the keys of KIND_TEXT."""
    kind = item.get("kind")
    return kind if kind in KIND_TEXT else "indicator"


def kind_icon(kind: str, danger: bool = False, size: int = 12) -> QtGui.QIcon:
    """A small marker for one kind, drawn in the ACTIVE theme's colours.

    Why painted and not a Unicode glyph: the lab PC's fonts decide what a glyph
    like a triangle looks like (or whether it exists at all), and a font glyph
    takes the text colour, not a theme colour. Painting it ourselves gives the
    same shape everywhere.

    Why SHAPE as well as colour: about one man in twelve cannot tell amber from
    green, so each kind also differs in form -- a filled dot (a knob you turn),
    a hollow ring (a lamp you look at), a triangle (a play button).

    The colours are read from COLORS at call time, so this must be called AFTER
    set_theme -- which is the case, because the tree is filled only once the
    window exists (suite gotcha #6: never cache a colour at import).
    """
    colour = {
        "control": C["accent"],       # amber = the colour of things you drive
        "indicator": C["ok"],         # green = a live readout / status lamp
        "action": C["danger"] if danger else C["text"],
    }.get(kind, C["ok"])

    # Draw at twice the size and tell Qt so: the icon stays sharp on a
    # high-DPI screen instead of being a blurry 12-pixel blob.
    ratio = 2
    pm = QtGui.QPixmap(size * ratio, size * ratio)
    pm.setDevicePixelRatio(ratio)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    q = QtGui.QColor(colour)
    m = size * 0.2                                   # margin around the mark
    box = QtCore.QRectF(m, m, size - 2 * m, size - 2 * m)
    if kind == "control":
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(q)
        p.drawEllipse(box)
    elif kind == "action":
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(q)
        p.drawPolygon(QtGui.QPolygonF([
            QtCore.QPointF(box.left() + 0.5, box.top()),
            QtCore.QPointF(box.right() + 0.5, box.center().y()),
            QtCore.QPointF(box.left() + 0.5, box.bottom())]))
    else:
        pen = QtGui.QPen(q)
        pen.setWidthF(1.6)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawEllipse(box.adjusted(0.8, 0.8, -0.8, -0.8))
    p.end()
    return QtGui.QIcon(pm)


def kind_legend() -> QtWidgets.QWidget:
    """One line under the tree: the three markers with a word each."""
    w = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(w)
    h.setContentsMargins(2, 0, 0, 0)
    h.setSpacing(4)
    for kind, (word, tip) in KIND_TEXT.items():
        mark = QtWidgets.QLabel()
        mark.setPixmap(kind_icon(kind).pixmap(12, 12))
        mark.setToolTip(tip)
        text = QtWidgets.QLabel(word)
        text.setToolTip(tip)
        text.setStyleSheet(f"color:{C['muted']}; font-size:10px;")
        h.addWidget(mark)
        h.addWidget(text)
        h.addSpacing(8)
    h.addStretch(1)
    return w


# --------------------------------------------------------------------------- #
# One widget per descriptor
# --------------------------------------------------------------------------- #

def _lock_icon(colour: str, closed: bool, size: int = 14) -> QtGui.QIcon:
    """A small padlock for a module's control state in the tree: closed =
    another PC has control, open = yours or free. A lock and not a dot, because
    the tree's rows already use dots for their KIND (set / read / run)."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    c = QtGui.QColor(colour)
    p.setPen(QtGui.QPen(c, 1.6))
    p.setBrush(QtCore.Qt.NoBrush)
    # the shackle: an arch over the body; lifted and shifted right when open
    dx, dy = (0, 0) if closed else (3, -2)
    p.drawArc(QtCore.QRectF(4 + dx, 1.5 + dy, 6, 8), 0, 180 * 16)
    p.drawLine(QtCore.QPointF(4 + dx, 5.5 + dy), QtCore.QPointF(4 + dx, 7))
    if closed:
        p.drawLine(QtCore.QPointF(10, 5.5), QtCore.QPointF(10, 7))
    p.setBrush(c)
    p.drawRoundedRect(QtCore.QRectF(2.5, 7, 9, 6), 1.2, 1.2)
    p.end()
    return QtGui.QIcon(pm)


class _FlowLayout(QtWidgets.QLayout):
    """Lays widgets out left to right and wraps to a new line when full (Qt's
    own "flow layout" example, trimmed) -- the control chips of ten modules
    stay one line on a wide window and two on a narrow one."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):                     # noqa: N802 (Qt names)
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):                         # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):                         # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):               # noqa: N802
        return QtCore.Qt.Orientations(0)

    def hasHeightForWidth(self):                 # noqa: N802
        return True

    def heightForWidth(self, width):             # noqa: N802
        return self._place(QtCore.QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):                 # noqa: N802
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self):                          # noqa: N802
        return self.minimumSize()

    def minimumSize(self):                       # noqa: N802
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect, move: bool) -> int:
        x, y, line_h, gap = rect.x(), rect.y(), 0, self.spacing()
        for item in self._items:
            w, h = item.sizeHint().width(), item.sizeHint().height()
            if x + w > rect.right() and line_h > 0:
                x, y, line_h = rect.x(), y + line_h + gap, 0
            if move:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), item.sizeHint()))
            x += w + gap
            line_h = max(line_h, h)
        return y + line_h - rect.y()


def _person(inst):
    """How a click on this panel reaches the service: as a PERSON ("gui"), not
    as the scan engine's "machine" (suite_common/control.py) -- a trainee's
    panel at another PC must not change what someone else controls. Test fakes
    without gui_command fall back to command."""
    return getattr(inst, "gui_command", None) or inst.command


class ItemWidget(QtWidgets.QWidget):
    """A single control, indicator or action, built from its descriptor."""

    def __init__(self, item: dict, on_log=None):
        super().__init__()
        self.item = item
        self.on_log = on_log or (lambda msg: None)
        self.history: list[float] = []
        self._last_value = None

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)

        label = QtWidgets.QLabel(item.get("label", item["id"]))
        label.setFixedWidth(150)
        label.setToolTip(item.get("help", ""))
        lay.addWidget(label)

        self.editor = None
        self.readout = None
        kind, dtype = item.get("kind"), item.get("type")

        if kind == "action":
            self._build_action(lay)
        elif kind == "control":
            self._build_control(lay, dtype)
        else:
            self._build_indicator(lay, dtype)

        lay.addStretch(1)
        unit = item.get("unit")
        if unit:
            u = QtWidgets.QLabel(unit)
            u.setStyleSheet(f"color:{C['muted']};")
            u.setFixedWidth(46)
            lay.addWidget(u)

    # ---- builders --------------------------------------------------------

    def _build_action(self, lay):
        btn = QtWidgets.QPushButton(self.item.get("label", self.item["id"]))
        btn.setObjectName("danger" if self.item.get("danger") else "primary")
        btn.setToolTip(self.item.get("help", ""))
        btn.clicked.connect(self._fire_action)
        lay.addWidget(btn)
        self.editor = btn

    def _build_control(self, lay, dtype):
        if dtype == "bool":
            box = QtWidgets.QCheckBox()
            # `.clicked` is user-only, so refreshing the panel from status
            # cannot loop back and re-send the value (suite gotcha #13).
            box.clicked.connect(lambda on: self._send(bool(on)))
            lay.addWidget(box)
            self.editor = box
            return

        if dtype == "enum":
            combo = QtWidgets.QComboBox()
            combo.addItems([str(o) for o in (self.item.get("options") or [])])
            combo.activated.connect(
                lambda *_: self._send(combo.currentText()))
            lay.addWidget(combo)
            self.editor = combo
            return

        if dtype == "string":
            edit = QtWidgets.QLineEdit()
            edit.returnPressed.connect(lambda: self._send(edit.text()))
            lay.addWidget(edit)
            self.editor = edit
            return

        spin = QtWidgets.QDoubleSpinBox()
        spin.setDecimals(int(self.item.get("decimals", 3)))
        spin.setSingleStep(float(self.item.get("step", 1.0) or 1.0))
        spin.setFixedWidth(110)
        self._apply_limits(spin)
        lay.addWidget(spin)
        btn = QtWidgets.QPushButton("Set")
        btn.setFixedWidth(46)
        btn.clicked.connect(lambda: self._send(spin.value()))
        lay.addWidget(btn)
        self.editor = spin

        self.readout = QtWidgets.QLabel("--")
        self.readout.setStyleSheet(f"color:{C['accent']};")
        self.readout.setFixedWidth(96)
        lay.addWidget(self.readout)

    def _build_indicator(self, lay, dtype):
        self.readout = QtWidgets.QLabel("--")
        self.readout.setStyleSheet(f"color:{C['accent']}; font-weight:700;")
        self.readout.setFixedWidth(130)
        lay.addWidget(self.readout)

    def _apply_limits(self, spin):
        lo, hi = self.item.get("min"), self.item.get("max")
        lo = float(lo) if lo is not None and math.isfinite(float(lo)) else -1e9
        hi = float(hi) if hi is not None and math.isfinite(float(hi)) else 1e9
        spin.setRange(lo, hi)
        spin.setToolTip(f"limits {lo:g} to {hi:g} {self.item.get('unit', '')}".strip())

    def refresh_limits(self, item: dict):
        """Re-clamp to a freshly-read descriptor. The bounds move at runtime."""
        self.item.update({k: item.get(k) for k in ("min", "max")})
        if isinstance(self.editor, QtWidgets.QDoubleSpinBox):
            self._apply_limits(self.editor)

    # ---- driving ---------------------------------------------------------

    def _send(self, value):
        spec = self.item.get("set")
        inst = self.item.get("_inst")
        param = self.item.get("_param")
        try:
            if inst is not None and spec:
                extra = dict(spec.get("extra") or {})
                scale = float(self.item.get("scale", 1.0))
                wire = value * scale if isinstance(value, (int, float)) \
                    and not isinstance(value, bool) else value
                _person(inst)(spec["verb"], **{spec["arg"]: wire}, **extra)
            elif param is not None:
                param.set(float(value))
            else:
                self.on_log(f"{self.item['pid']}: nothing to drive it with")
                return
            self.on_log(f"{self.item['pid']} <- {value}")
        except Exception as exc:
            self.on_log(f"{self.item['pid']}: {exc}")

    def _fire_action(self):
        item = self.item
        args = item.get("args") or []
        values = {}
        if args:
            dlg = ActionDialog(item, self)
            if dlg.exec() != QtWidgets.QDialog.Accepted:
                return
            values = dlg.values()
        elif item.get("danger"):
            # Confirm only when there is no dialog to pause at anyway. Demag,
            # a datum reset and a Home all move real hardware.
            ok = QtWidgets.QMessageBox.question(
                self, "Confirm",
                f"Run '{item.get('label', item['id'])}' on {item['module']}?")
            if ok != QtWidgets.QMessageBox.Yes:
                return
        inst = item.get("_inst")
        if inst is None:
            self.on_log(f"{item['pid']}: no connection")
            return
        try:
            _person(inst)(item["id"], **values)
            self.on_log(f"{item['pid']}()" + (f" {values}" if values else ""))
        except Exception as exc:
            self.on_log(f"{item['pid']}: {exc}")

    # ---- refreshing ------------------------------------------------------

    def update_from(self, status: dict):
        """Show the current value, from the descriptor's read_path."""
        path = self.item.get("read_path")
        if not path:
            return
        value = status
        for key in path:
            if isinstance(key, int) and isinstance(value, (list, tuple)):
                if not -len(value) <= key < len(value):
                    return
                value = value[key]
                continue
            if not isinstance(value, dict) or key not in value:
                return
            value = value[key]

        scale = float(self.item.get("scale", 1.0))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            shown = value / scale
            self._last_value = shown
            if self.item.get("plottable"):
                self.history.append(shown)
                del self.history[:-HISTORY]
            text = f"{shown:.{int(self.item.get('decimals', 3))}f}"
        else:
            self._last_value = value
            text = str(value)

        if self.readout is not None:
            self.readout.setText(text)

        # Reflect the instrument's own state into a bool/enum editor without
        # re-sending it: blockSignals so setChecked cannot look like a click.
        ed = self.editor
        if isinstance(ed, QtWidgets.QCheckBox):
            ed.blockSignals(True); ed.setChecked(bool(value)); ed.blockSignals(False)
        elif isinstance(ed, QtWidgets.QComboBox) and isinstance(value, str):
            i = ed.findText(value)
            if i >= 0 and i != ed.currentIndex():
                ed.blockSignals(True); ed.setCurrentIndex(i); ed.blockSignals(False)

    def poll_local(self):
        """Read a simulated registry Parameter directly (no wire, no status)."""
        param = self.item.get("_param")
        if param is None:
            return
        try:
            value = param.get()
        except Exception:
            return
        if isinstance(value, np.ndarray):
            return                                   # arrays are not readouts
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return
        self._last_value = value
        if self.item.get("plottable"):
            self.history.append(float(value))
            del self.history[:-HISTORY]
        if self.readout is not None:
            self.readout.setText(f"{float(value):.4g}")


class ActionDialog(QtWidgets.QDialog):
    """Collects an action's declared arguments before firing it."""

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(item.get("label", item["id"]))
        self._fields = {}
        form = QtWidgets.QFormLayout(self)

        if item.get("help"):
            note = QtWidgets.QLabel(item["help"])
            note.setWordWrap(True); note.setStyleSheet(f"color:{C['muted']};")
            note.setFixedWidth(340)
            form.addRow(note)

        for arg in item.get("args") or []:
            w = self._field_for(arg)
            self._fields[arg["name"]] = w
            label = arg.get("label", arg["name"])
            if arg.get("unit"):
                label += f" [{arg['unit']}]"
            form.addRow(label, w)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if item.get("danger"):
            buttons.button(QtWidgets.QDialogButtonBox.Ok).setObjectName("danger")
        form.addRow(buttons)

    def _field_for(self, arg):
        t = arg.get("type", "float")
        if t == "int":
            w = QtWidgets.QSpinBox()
            w.setRange(int(arg.get("min", -10**9)), int(arg.get("max", 10**9)))
            if arg.get("default") is not None:
                w.setValue(int(arg["default"]))
            return w
        if t == "bool":
            w = QtWidgets.QCheckBox()
            w.setChecked(bool(arg.get("default")))
            return w
        if t == "string":
            w = QtWidgets.QLineEdit(str(arg.get("default") or ""))
            return w
        w = QtWidgets.QDoubleSpinBox()
        w.setDecimals(3)
        w.setRange(float(arg.get("min", -1e9)), float(arg.get("max", 1e9)))
        if arg.get("default") is not None:
            w.setValue(float(arg["default"]))
        return w

    def values(self) -> dict:
        out = {}
        for name, w in self._fields.items():
            if isinstance(w, QtWidgets.QCheckBox):
                out[name] = w.isChecked()
            elif isinstance(w, QtWidgets.QLineEdit):
                out[name] = w.text()
            else:
                out[name] = w.value()
        return out


# --------------------------------------------------------------------------- #
# The tab
# --------------------------------------------------------------------------- #

class ControlPanel(QtWidgets.QWidget):
    """Pick parameters from whatever is connected; drive them; watch them."""

    def __init__(self, on_log=None):
        super().__init__()
        self.on_log = on_log or (lambda msg: None)
        self.items: dict[str, dict] = {}        # pid -> descriptor
        self.widgets: dict[str, ItemWidget] = {}
        self.curves: dict[str, object] = {}
        self.lab = None
        self.registry = None
        self._revs: dict[str, int] = {}         # module -> last describe_rev
        # Traces the operator switched off by clicking their legend entry. Kept
        # HERE, not only on the curves, because every tick rebuilds the plot
        # from scratch: without this set, ticking one more parameter would bring
        # every hidden trace back. A layout saves and restores it too.
        self.hidden: set[str] = set()
        self.layouts = _load_layouts()
        self._tree_tops: dict[str, QtWidgets.QTreeWidgetItem] = {}   # module -> tree node

        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        outer.addWidget(self._build_picker(), 0)
        outer.addWidget(self._build_panel(), 1)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(250)                   # 4 Hz; services publish 5-10

    # ---- construction ----------------------------------------------------

    def _build_picker(self):
        card = QtWidgets.QFrame(); card.setObjectName("card"); card.setFixedWidth(280)
        v = QtWidgets.QVBoxLayout(card); v.setContentsMargins(12, 12, 12, 12); v.setSpacing(8)

        tag = QtWidgets.QLabel("AVAILABLE  -  tick to show")
        tag.setStyleSheet(f"color:{C['muted']}; font-size:10px; font-weight:700;")
        v.addWidget(tag)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemChanged.connect(self._on_tick)
        v.addWidget(self.tree, 1)
        # "Find focus" (a button), "Stable" (a readout) and "Scan point X" (a
        # value you set) otherwise look identical in the tree; the marker in
        # front of each name says which, and this line says what they mean.
        v.addWidget(kind_legend())

        self.layout_combo = QtWidgets.QComboBox()
        self.layout_combo.setEditable(True)
        self.layout_combo.addItems(sorted(self.layouts))
        v.addWidget(self.layout_combo)

        btns = QtWidgets.QHBoxLayout()
        load = QtWidgets.QPushButton("Load"); load.clicked.connect(self._load_layout)
        save = QtWidgets.QPushButton("Save"); save.setObjectName("primary")
        save.clicked.connect(self._save_layout)
        drop = QtWidgets.QPushButton("Delete"); drop.clicked.connect(self._delete_layout)
        for b in (load, save, drop):
            btns.addWidget(b)
        v.addLayout(btns)

        hint = QtWidgets.QLabel("Layouts are per setup: an alignment panel and "
                                "an FMR panel want different knobs.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C['muted']}; font-size:10px;")
        v.addWidget(hint)
        return card

    def _build_panel(self):
        card = QtWidgets.QFrame(); card.setObjectName("card")
        v = QtWidgets.QVBoxLayout(card); v.setContentsMargins(12, 12, 12, 12); v.setSpacing(8)

        self.empty = QtWidgets.QLabel(
            "Nothing selected yet.\n\nConnect instruments on the Settings tab, "
            "then tick parameters on the left.")
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setStyleSheet(f"color:{C['muted']};")

        # ONE strip of small chips, one per connected module: who has control
        # (suite_common/control.py). Lukas: a line per module "takes quite some
        # space for more modules". Click a chip = Take control / Release; the
        # tooltip says who holds it and whether a scan runs. Wraps when full.
        strip = QtWidgets.QWidget()
        self.control_lay = _FlowLayout(strip, spacing=6)
        v.addWidget(strip)                 # at the TOP, above everything else
        v.addWidget(self.empty)
        self.control_strip = strip
        self.control_chips: dict[str, QtWidgets.QToolButton] = {}
        self._control_seen: dict[str, dict] = {}     # module -> last control state
        self._chip_key: dict[str, tuple] = {}        # module -> what the chip shows

        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        inner = QtWidgets.QWidget()
        self.groups_lay = QtWidgets.QVBoxLayout(inner)
        self.groups_lay.setContentsMargins(0, 0, 0, 0)
        self.groups_lay.setSpacing(10)
        self.groups_lay.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 3)
        self.scroll = scroll

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "samples")
        self.plot.setLabel("left", "each trace normalised to its own range")
        self.plot.setYRange(-0.05, 1.05)
        self.plot.addLegend()
        self.plot.setMinimumHeight(170)
        v.addWidget(self.plot, 2)
        return card

    # ---- what we are connected to ---------------------------------------

    def set_source(self, registry=None, lab=None, prefix: bool = True):
        """Point the panel at a live lab, or at a simulated registry."""
        self.registry, self.lab = registry, lab
        for chip in self.control_chips.values():
            self.control_lay.removeWidget(chip)
            chip.setParent(None)
        self.control_chips.clear()
        self._control_seen.clear()
        self._chip_key.clear()
        if lab is not None:
            # this panel is a GUI to every module: say "still here", so the
            # module lists it and a control taken here does not lapse
            for inst in lab.instruments.values():
                start = getattr(inst, "start_gui_heartbeat", None)
                if start is not None:
                    start()
            items = items_from_lab(lab, prefix)
        elif registry is not None:
            items = items_from_registry(registry)
        else:
            items = []
        self.items = {i["pid"]: i for i in items}
        self._revs.clear()
        self._reload_tree()
        self._rebuild_panel()

    def _reload_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        # One icon per (kind, danger), built now -- i.e. after set_theme -- and
        # shared by every row, rather than painting a pixmap per parameter.
        icons = {(k, d): kind_icon(k, d) for k in KIND_TEXT for d in (False, True)}
        by_module: dict[str, dict[str, list]] = {}
        for item in self.items.values():
            group = item.get("group") or "Other"
            by_module.setdefault(item["module"], {}).setdefault(group, []).append(item)

        self._tree_tops = {}
        self._chip_key.clear()           # the new tree nodes need their dot again
        for module in sorted(by_module):
            top = QtWidgets.QTreeWidgetItem([module])
            self._tree_tops[module] = top
            top.setFlags(QtCore.Qt.ItemIsEnabled)
            font = top.font(0); font.setBold(True); top.setFont(0, font)
            self.tree.addTopLevelItem(top)
            for group in sorted(by_module[module]):
                gnode = QtWidgets.QTreeWidgetItem([group])
                gnode.setFlags(QtCore.Qt.ItemIsEnabled)
                gnode.setForeground(0, QtGui.QColor(C["muted"]))
                top.addChild(gnode)
                for item in sorted(by_module[module][group],
                                   key=lambda d: (d.get("order", 0), d["id"])):
                    node = QtWidgets.QTreeWidgetItem([item.get("label", item["id"])])
                    node.setFlags(node.flags() | QtCore.Qt.ItemIsUserCheckable)
                    node.setCheckState(0, QtCore.Qt.Unchecked)
                    node.setData(0, QtCore.Qt.UserRole, item["pid"])
                    kind = kind_of(item)
                    node.setData(0, KIND_ROLE, kind)
                    node.setIcon(0, icons[(kind, bool(item.get("danger")))])
                    # The tooltip leads with the kind, then the module's own help.
                    tip = KIND_TEXT[kind][1]
                    if kind == "action" and item.get("danger"):
                        tip += " Moves hardware: it asks before running."
                    if item.get("help"):
                        tip += "\n" + item["help"]
                    node.setToolTip(0, tip)
                    gnode.addChild(node)
            top.setExpanded(True)
        self.tree.blockSignals(False)

    # ---- the panel itself ------------------------------------------------

    def selected_pids(self) -> list[str]:
        out = []
        it = QtWidgets.QTreeWidgetItemIterator(
            self.tree, QtWidgets.QTreeWidgetItemIterator.Checked)
        while it.value():
            pid = it.value().data(0, QtCore.Qt.UserRole)
            if pid:
                out.append(pid)
            it += 1
        return out

    def _on_tick(self, *_):
        self._rebuild_panel()

    def _sync_hidden(self):
        """Read the legend's on/off state off the curves into `self.hidden`.

        pyqtgraph's legend hides a curve by calling setVisible(False) on it when
        its entry is clicked, and tells nobody. So the curves ARE the truth for
        whatever is plotted now. Parameters not plotted right now keep the state
        they had (untick and re-tick a hidden trace: it comes back hidden).
        """
        for pid, curve in self.curves.items():
            if curve.isVisible():
                self.hidden.discard(pid)
            else:
                self.hidden.add(pid)

    def hidden_traces(self) -> list[str]:
        """The plotted traces that are switched off, in a stable order."""
        self._sync_hidden()
        return sorted(pid for pid in self.curves if pid in self.hidden)

    def _rebuild_panel(self, hidden=None):
        """Rebuild the widgets and the strip chart from the ticked parameters.

        `hidden` (a set of pids) REPLACES the remembered hidden traces -- that
        is what loading a layout does. Left as None, the current legend state is
        carried over, so ticking one more parameter does not unhide the others.
        """
        if hidden is None:
            self._sync_hidden()
        else:
            self.hidden = set(hidden)
        for w in list(self.widgets.values()):
            w.setParent(None)
        self.widgets.clear()
        self.curves.clear()
        self.plot.clear()
        self.plot.addLegend()

        while self.groups_lay.count() > 1:              # keep the stretch
            child = self.groups_lay.takeAt(0)
            if child.widget():
                child.widget().setParent(None)

        pids = self.selected_pids()
        self.empty.setVisible(not pids)
        self.scroll.setVisible(bool(pids))

        chosen = [self.items[p] for p in pids if p in self.items]
        by_group: dict[str, list] = {}
        for item in chosen:
            title = item["module"] + "  -  " + (item.get("group") or "Other")
            by_group.setdefault(title, []).append(item)

        colours = ["#ff9e2c", "#3ddc84", "#5ba8ff", "#ff5c5c", "#c58bff", "#ffd166"]
        n = 0
        for title in sorted(by_group):
            box = QtWidgets.QGroupBox(title)
            gl = QtWidgets.QVBoxLayout(box); gl.setSpacing(2)
            for item in sorted(by_group[title],
                               key=lambda d: (d.get("order", 0), d["id"])):
                w = ItemWidget(item, on_log=self.on_log)
                self.widgets[item["pid"]] = w
                gl.addWidget(w)
                if item.get("plottable"):
                    pen = pg.mkPen(colours[n % len(colours)], width=2)
                    curve = self.plot.plot(
                        [], [], pen=pen, name=item.get("label", item["id"]))
                    # Hidden before it is ever drawn; the legend entry still
                    # exists (greyed), so one click brings it back.
                    curve.setVisible(item["pid"] not in self.hidden)
                    self.curves[item["pid"]] = curve
                    n += 1
            self.groups_lay.insertWidget(self.groups_lay.count() - 1, box)

        self.plot.setVisible(bool(self.curves))

    # ---- polling ---------------------------------------------------------

    def _refresh(self):
        # with a live lab, poll even with nothing ticked: the control chips
        # show every connected module
        if not self.widgets and self.lab is None:
            return
        if self.lab is not None:
            self._refresh_remote()
        else:
            for w in self.widgets.values():
                w.poll_local()
        self._redraw_traces()

    def _redraw_traces(self):
        """Draw every trace NORMALISED to its own range over the window.

        A control panel plots whatever you ticked, and those do not share a
        unit: RF frequency in MHz sits at 1000 while the magnetic field sits at
        0.1, and on a shared axis the field is a flat line on the floor. Nothing
        is wrong with the data -- the plot is just unreadable, which is worse,
        because it looks like the signal is dead.

        So each trace is scaled to 0..1 across its own min..max. What you read
        off this plot is SHAPE -- settling, drift, oscillation, several channels
        at once. The absolute numbers are in the readouts a few centimetres
        away, and the legend carries the live value so the two are never far
        apart.
        """
        for pid, curve in self.curves.items():
            w = self.widgets.get(pid)
            if w is None or not w.history:
                continue
            y = np.asarray(w.history, dtype=float)
            finite = y[np.isfinite(y)]
            if finite.size == 0:
                continue
            lo, hi = float(finite.min()), float(finite.max())
            span = hi - lo
            # A dead-flat trace has no range to normalise into; park it in the
            # middle rather than dividing by zero or pinning it to the axis.
            norm = (y - lo) / span if span > 1e-12 else np.full_like(y, 0.5)
            curve.setData(np.arange(y.size, dtype=float), norm)

            label = w.item.get("label", w.item["id"])
            unit = w.item.get("unit") or ""
            self._set_legend_text(pid, f"{label}  {y[-1]:.4g} {unit}".strip())

    def _set_legend_text(self, pid, text):
        """Retitle one legend entry in place (pyqtgraph has no public setter)."""
        legend = getattr(self.plot.plotItem, "legend", None)
        curve = self.curves.get(pid)
        if legend is None or curve is None:
            return
        for sample, label in getattr(legend, "items", []):
            if getattr(sample, "item", None) is curve:
                label.setText(text)
                return

    def _refresh_remote(self):
        for name, inst in self.lab.instruments.items():
            try:
                # The FRESH cache only, never a request: this runs on the GUI
                # thread, and since 2026-09-28 `status()` asks a silent service
                # directly -- a dead one would freeze the window for the whole
                # REQ timeout at every poll. A stale cache means no update.
                # (Right after connecting no frame has arrived YET; the service
                # has just answered `info`, so asking once is quick.)
                latest = getattr(inst, "latest", None)
                status = latest() if latest is not None else inst.status()
                if status is None and inst.status_age() is None:
                    status = inst.status()
            except Exception:
                status = None
            if status is None:
                continue          # one dropped service must not stop the rest
                                  # of the panel from updating
            manifest = getattr(inst, "manifest", None) or {}
            module = getattr(inst, "alias", None) or manifest.get("module", name)
            for pid, w in self.widgets.items():
                if w.item["module"] == module:
                    w.update_from(status)
            self._update_control(module, inst, status.get("control"))

            # LIMITS MOVE. One integer compare per module per poll, and a
            # manifest re-read only when it actually changed.
            rev = status.get("describe_rev")
            if rev is not None and rev != self._revs.get(module):
                first = module not in self._revs
                self._revs[module] = rev
                if not first:
                    self._reread_limits(inst, module)

    # ---- control: who may change this module (suite_common/control.py) ----

    def _update_control(self, module, inst, ctl):
        """Show who controls `module`, and grey its knobs if it is another PC.

        Buttons whose verb the service always accepts (ctl["always"]: STOP,
        Kill AF, ...) stay usable, as in a module's own viewer window.
        Indicators are never greyed -- they only show.
        """
        me = getattr(inst, "gui_identity", None)
        if not isinstance(ctl, dict) or me is None:
            # a module without control (yet): a grey chip that says so
            self._control_seen.pop(module, None)
            self._show_chip(module, inst, "none", [
                f"{module}: this module has no control yet -- anyone connected "
                "may change it, and a scan cannot claim it"])
            return
        self._control_seen[module] = ctl         # what the chip's menu acts on
        holder = ctl.get("holder")
        mine = bool(holder) and (holder.get("id") == me["id"] or same_pc(holder, me))
        viewer = bool(holder) and not mine
        if mine:
            lines = [f"{module}: you have control (this PC)"]
        elif holder:
            since = time.strftime("%H:%M", time.localtime(holder.get("since", 0)))
            lines = [f"{module}: VIEWER — {describe_holder(holder)} has control "
                     f"since {since}"]
        else:
            lines = [f"{module}: nobody has control — changes allowed"]
        scan = ctl.get("scan")
        if scan:
            pc = str(scan.get("host") or "?").rpartition("@")[2]
            lines.append(f"scan '{scan.get('label')}' running ({pc})")
        driving = [c.get("name") or "a program" for c in ctl.get("clients", [])
                   if c.get("kind") == "machine" and c.get("driving")
                   and c.get("id") != (scan or {}).get("id")]
        if driving:
            lines.append("also driving: " + ", ".join(driving))
        self._show_chip(module, inst, "mine" if mine else "viewer" if viewer else "free",
                        lines, running=bool(scan))

        always = set(ctl.get("always") or [])
        for w in self.widgets.values():
            item = w.item
            if item["module"] != module or item.get("kind") == "indicator":
                continue
            if item.get("kind") == "action":
                w.setEnabled(not viewer or item["id"] in always)
            else:
                w.setEnabled(not viewer)

    #: chip state -> (dot, colour key, meaning). Colours from the theme, so they
    #: read in both palettes; the dot's SHAPE differs too, not only its colour.
    CHIP_STATES = {"mine": ("●", "ok", "you have control"),
                   "viewer": ("◆", "accent", "another PC has control"),
                   "free": ("○", "muted", "nobody has control"),
                   "none": ("○", "muted", "no control in this module")}

    def _show_chip(self, module, inst, state, lines, running=False):
        """The chip for `module` in the strip, and the dot in the tree.

        Rebuilt only when what it shows changes: a menu rebuilt at the poll
        rate would close under the user's mouse."""
        key = (state, tuple(lines), running)
        if self._chip_key.get(module) == key:
            return
        self._chip_key[module] = key
        chip = self.control_chips.get(module)
        if chip is None:
            chip = QtWidgets.QToolButton()
            chip.setPopupMode(QtWidgets.QToolButton.InstantPopup)
            chip.setMenu(QtWidgets.QMenu(chip))
            self.control_lay.addWidget(chip)
            self.control_chips[module] = chip
        dot, colour, _meaning = self.CHIP_STATES[state]
        short = module.split("_", 1)[0]            # "kim_130_233_..." -> "kim"
        chip.setText(f"{dot} {short}" + ("  ▶" if running else ""))
        chip.setProperty("state", state)
        chip.setToolTip("\n".join(lines))
        chip.setStyleSheet(
            f"QToolButton {{ color:{C[colour]}; border:1px solid {C['border']};"
            f" border-radius:9px; padding:1px 8px; font-weight:700; }}"
            "QToolButton::menu-indicator { image: none; }")
        menu = chip.menu()
        menu.clear()
        for line in lines:
            a = menu.addAction(line)
            a.setEnabled(False)
        if state != "none":
            menu.addSeparator()
            act = menu.addAction("Release" if state == "mine" else "Take control")
            act.triggered.connect(lambda _=False, m=module, i=inst:
                                  self._toggle_control(m, i))
        # a padlock on the module's name in the AVAILABLE tree
        top = self._tree_tops.get(module)
        if top is not None:
            # signals blocked: changing an item's icon emits itemChanged, which
            # the panel reads as a tick and REBUILDS every widget (found by
            # test_control_tab_reclamps_when_the_limits_move)
            self.tree.blockSignals(True)
            try:
                top.setIcon(0, _lock_icon(C[colour], closed=state == "viewer"))
                top.setToolTip(0, "\n".join(lines))
            finally:
                self.tree.blockSignals(False)

    def _toggle_control(self, module, inst):
        """Take control of (or release) one module, as a person would.

        Acts on the state the row SHOWS (the user decided on that), not on a
        fresh read that could differ from what they saw."""
        holder = (self._control_seen.get(module) or {}).get("holder")
        me = inst.gui_identity
        mine = bool(holder) and (holder.get("id") == me["id"] or same_pc(holder, me))
        try:
            if mine:
                inst.gui_command("release_control")
                self.on_log(f"{module}: control released")
                return
            force = False
            if holder:
                since = time.strftime("%H:%M", time.localtime(holder.get("since", 0)))
                ans = QtWidgets.QMessageBox.question(
                    self, "Take control",
                    f"{describe_holder(holder)} has control of {module} since "
                    f"{since}.\n\nTake it over? That PC becomes a viewer (it sees "
                    "that you took it) and can take it back the same way.")
                if ans != QtWidgets.QMessageBox.Yes:
                    return
                force = True
            r = inst.gui_command("take_control", force=force)
            self.on_log(f"{module}: " + ("you have control" if r.get("granted")
                                         else "someone else took it first"))
        except Exception as exc:
            self.on_log(f"{module}: {exc}")

    def _reread_limits(self, inst, module):
        try:
            fresh = inst.command("describe").get("describe") or {}
        except Exception:
            return
        inst.manifest = fresh
        prefix = any("." in pid for pid in self.items)
        for d in fresh.get("parameters", []):
            pid = (module + "." + d["id"]) if prefix else d["id"]
            if pid in self.items:
                self.items[pid].update(d)
            w = self.widgets.get(pid)
            if w is not None:
                w.refresh_limits(d)
        self.on_log(module + ": limits re-read (describe_rev changed)")

    # ---- layouts ---------------------------------------------------------

    def _load_layout(self):
        name = self.layout_combo.currentText().strip()
        entry = self.layouts.get(name)
        pids = set(layout_pids(entry))
        if not pids:
            self.on_log("layout '" + name + "' is empty or unknown")
            return
        self.tree.blockSignals(True)
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            node = it.value()
            pid = node.data(0, QtCore.Qt.UserRole)
            if pid:
                node.setCheckState(0, QtCore.Qt.Checked if pid in pids
                                   else QtCore.Qt.Unchecked)
            it += 1
        self.tree.blockSignals(False)
        # The layout's hidden traces replace whatever was hidden before; an old
        # layout without the field shows every trace, as it always did.
        self._rebuild_panel(hidden=set(layout_hidden(entry)))

        missing = pids - set(self.items)
        if missing:
            # Say so rather than silently showing a smaller panel than the one
            # that was saved: it usually means a module is not connected.
            shown = ", ".join(sorted(missing)[:3])
            self.on_log(f"layout '{name}': {len(missing)} parameter(s) not "
                        f"available right now ({shown})")
        self.on_log("layout '" + name + "' loaded")

    def _save_layout(self):
        name = self.layout_combo.currentText().strip()
        if not name:
            self.on_log("give the layout a name first")
            return
        pids = self.selected_pids()
        hidden = self.hidden_traces()
        self.layouts[name] = {"pids": pids, "hidden": hidden}
        _save_layouts(self.layouts)
        if self.layout_combo.findText(name) < 0:
            self.layout_combo.addItem(name)
        extra = f", {len(hidden)} trace(s) hidden" if hidden else ""
        self.on_log(f"layout '{name}' saved ({len(pids)} items{extra})")

    def _delete_layout(self):
        name = self.layout_combo.currentText().strip()
        if name in self.layouts:
            del self.layouts[name]
            _save_layouts(self.layouts)
            i = self.layout_combo.findText(name)
            if i >= 0:
                self.layout_combo.removeItem(i)
            self.on_log("layout '" + name + "' deleted")


# A layout on disk is either
#   ["pid", "pid", ...]                               (before 2026-09-25), or
#   {"pids": ["pid", ...], "hidden": ["pid", ...]}    (since: + hidden traces).
# Both are read; only the second is written. An old suite_layouts.json must keep
# loading -- it is this PC's collection of panels, and nobody wants to rebuild it.

def layout_pids(entry) -> list[str]:
    """The ticked parameters of one stored layout, old or new format."""
    if isinstance(entry, dict):
        return list(entry.get("pids") or [])
    return list(entry or [])


def layout_hidden(entry) -> list[str]:
    """The hidden traces of one stored layout ([] for the old format)."""
    if isinstance(entry, dict):
        return list(entry.get("hidden") or [])
    return []


def _load_layouts() -> dict:
    try:
        return json.loads(LAYOUTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}          # missing or corrupt: start empty rather than refuse


def _save_layouts(layouts: dict) -> None:
    try:
        LAYOUTS_PATH.write_text(json.dumps(layouts, indent=2), encoding="utf-8")
    except OSError:
        pass               # a read-only checkout must not break the panel
