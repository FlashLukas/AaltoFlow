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

from apps.theme import C

#: Where the chosen panel layouts live. Next to the project, like
#: mission-control's profiles.json, and gitignored for the same reason: it is
#: this machine's preference, not part of the software.
LAYOUTS_PATH = Path(__file__).resolve().parent.parent / "suite_layouts.json"

#: How many points a strip chart keeps. At a 4 Hz poll this is ~4 minutes,
#: which is long enough to see a magnet settle and short enough to stay cheap.
HISTORY = 1000


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
# One widget per descriptor
# --------------------------------------------------------------------------- #

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
                inst.command(spec["verb"], **{spec["arg"]: wire}, **extra)
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
            inst.command(item["id"], **values)
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
        self.layouts = _load_layouts()

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
        v.addWidget(self.empty)

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
        if lab is not None:
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
        by_module: dict[str, dict[str, list]] = {}
        for item in self.items.values():
            group = item.get("group") or "Other"
            by_module.setdefault(item["module"], {}).setdefault(group, []).append(item)

        for module in sorted(by_module):
            top = QtWidgets.QTreeWidgetItem([module])
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
                    node.setToolTip(0, item.get("help", ""))
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

    def _rebuild_panel(self):
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
                    self.curves[item["pid"]] = self.plot.plot(
                        [], [], pen=pen, name=item.get("label", item["id"]))
                    n += 1
            self.groups_lay.insertWidget(self.groups_lay.count() - 1, box)

        self.plot.setVisible(bool(self.curves))

    # ---- polling ---------------------------------------------------------

    def _refresh(self):
        if not self.widgets:
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
                status = inst.status()
            except Exception:
                continue          # one dropped service must not stop the rest
                                  # of the panel from updating
            manifest = getattr(inst, "manifest", None) or {}
            module = getattr(inst, "alias", None) or manifest.get("module", name)
            for pid, w in self.widgets.items():
                if w.item["module"] == module:
                    w.update_from(status)

            # LIMITS MOVE. One integer compare per module per poll, and a
            # manifest re-read only when it actually changed.
            rev = status.get("describe_rev")
            if rev is not None and rev != self._revs.get(module):
                first = module not in self._revs
                self._revs[module] = rev
                if not first:
                    self._reread_limits(inst, module)

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
        pids = set(self.layouts.get(name) or [])
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
        self._rebuild_panel()

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
        self.layouts[name] = self.selected_pids()
        _save_layouts(self.layouts)
        if self.layout_combo.findText(name) < 0:
            self.layout_combo.addItem(name)
        self.on_log(f"layout '{name}' saved ({len(self.layouts[name])} items)")

    def _delete_layout(self):
        name = self.layout_combo.currentText().strip()
        if name in self.layouts:
            del self.layouts[name]
            _save_layouts(self.layouts)
            i = self.layout_combo.findText(name)
            if i >= 0:
                self.layout_combo.removeItem(i)
            self.on_log("layout '" + name + "' deleted")


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
