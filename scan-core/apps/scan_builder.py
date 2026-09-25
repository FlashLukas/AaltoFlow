"""
scan_builder.py — assemble an N-D scan from the registry, run it, see it.

Left  : palette of settables (add as an axis) and gettables (tick as detectors),
        populated straight from the registry → new instruments appear here for free.
        Both are TREES: one branch per service, its parameters underneath.
Middle: the axis STACK, outer→inner. Reorder to change nesting; there is no
        loop-count limit. Each row edits its sweep (start/stop/points).
Right : live summary (dimensions, total points, ETA), Run/Abort + progress, and
        the N-D result viewer (apps/data_view.py): pick the two dims to plot,
        hold or average every other one.

Everything the UI builds is a Recipe (recipe.py) — Save/Load is just YAML. The
engine (engine.py) does the actual sweeping; this file is only the cockpit.
"""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime
import sys
from pathlib import Path

import math
import time

import numpy as np
import xarray as xr
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # find scan_core
from scan_core import Recipe, build_sim_registry, run
from scan_core.errors import RoutineError, ScanAborted
from scan_core.preview import preview_axis, step_summary
from scan_core import scan_queue
from suite_common import title as suite_title
from apps.data_view import DataView
from apps.theme import DEFAULT_THEME, C, apply, set_theme

pg.setConfigOptions(antialias=True, imageAxisOrder="row-major", background=C["code_bg"])


# ─────────────────────────────── one axis row ─────────────────────────────────

#: Stand-in span for a parameter that advertises no limits. Arbitrary on
#: purpose and finite on purpose: the label says "no limits advertised"
#: so the number is visibly a placeholder to type over, not a real bound.
_UNBOUNDED = 1e6

#: Branch title for parameters with no module prefix (the simulated registry).
SIM_GROUP = "Simulator"


def split_id(pid: str) -> tuple[str, str]:
    """'pm16.power' -> ('pm16', 'power'); an unprefixed id -> ('', id).

    A live registry names every parameter `<module>.<id>` (the suite builds it
    with prefix=True, because several modules own a knob called `position`), so
    the prefix IS the service a parameter belongs to. The simulated registry has
    no prefixes and becomes one branch of its own.
    """
    module, dot, rest = pid.partition(".")
    return (module, rest) if dot else ("", pid)


def group_by_module(params) -> dict[str, list]:
    """Parameters grouped by service, in first-seen order (the registry's)."""
    groups: dict[str, list] = {}
    for p in params:
        groups.setdefault(split_id(p.id)[0], []).append(p)
    return groups


class AxisRow(QtWidgets.QFrame):
    changed = QtCore.Signal()
    remove = QtCore.Signal(object)
    move = QtCore.Signal(object, int)      # (self, +1/-1)
    preview = QtCore.Signal(object)        # double-click: show the actual setpoints

    def __init__(self, param, level_getter):
        super().__init__()
        self.param = param
        self.raw = None                    # set for non-editable (raster/zip) rows
        self._level_getter = level_getter
        self.setObjectName("axis")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6); lay.setSpacing(8)

        self.level_lbl = QtWidgets.QLabel("0")
        self.level_lbl.setStyleSheet(f"color:{C['accent']}; font-weight:800;")
        self.level_lbl.setFixedWidth(16)
        lay.addWidget(self.level_lbl)

        # Name on top, the live limit envelope as a caption beneath it. The
        # caption goes HERE rather than further along the row because the row is
        # already at the width of its column -- anything added on the right is
        # simply pushed out of sight.
        namebox = QtWidgets.QVBoxLayout(); namebox.setSpacing(0)
        name = QtWidgets.QLabel(f"{param.label}")
        name.setStyleSheet("font-weight:700;"); name.setFixedWidth(164)
        name.setToolTip(param.id)
        namebox.addWidget(name)
        self.limits_lbl = QtWidgets.QLabel()
        self.limits_lbl.setStyleSheet(f"color:{C['muted']}; font-size:10px;")
        self.limits_lbl.setFixedWidth(164)
        namebox.addWidget(self.limits_lbl)
        lay.addLayout(namebox)

        unit = QtWidgets.QLabel(f"[{param.unit}]"); unit.setStyleSheet(f"color:{C['muted']};")
        unit.setFixedWidth(44); lay.addWidget(unit)

        lo, hi = self._finite_limits()
        start, stop = self._default_span(lo, hi)
        self.integer = bool(getattr(param, "integer", False))
        self.start = self._spin(lo, hi, start)
        self.stop = self._spin(lo, hi, stop)
        self.num = QtWidgets.QSpinBox(); self.num.setRange(1, 100000)
        # An INT parameter (a scan-array index, a filter order) gets ONE POINT
        # PER VALUE by default. The old fixed 21 points across 0..19 asked for
        # 0.95, 1.9, ... which the service rounds -- so the setpoint it echoes
        # never matches and the point waits out its settle timeout.
        self.num.setValue(int(round(stop - start)) + 1 if self.integer else 21)
        self._sync_int_points()           # and cap it at one point per value
        self.num.valueChanged.connect(lambda *_: self.changed.emit())
        for w, t in ((self.start, "from"), (self.stop, "to"), (self.num, "pts")):
            box = QtWidgets.QVBoxLayout(); box.setSpacing(0)
            tl = QtWidgets.QLabel(t); tl.setStyleSheet(f"color:{C['muted']}; font-size:10px;")
            box.addWidget(tl); box.addWidget(w); lay.addLayout(box)

        # A spin box that silently refuses to go above 160 is baffling unless
        # you can see that 160 is the closed-loop ceiling -- and these limits
        # MOVE (piezo CL/OL, kim's leash, clMag's calibration), so showing the
        # number beats making the operator guess.
        self._sync_limits_label()

        lay.addStretch(1)
        up = QtWidgets.QPushButton("↑"); dn = QtWidgets.QPushButton("↓")
        rm = QtWidgets.QPushButton("✕"); rm.setObjectName("danger")
        for b in (up, dn, rm):
            b.setFixedWidth(30)
        up.clicked.connect(lambda: self.move.emit(self, -1))
        dn.clicked.connect(lambda: self.move.emit(self, +1))
        rm.clicked.connect(lambda: self.remove.emit(self))
        lay.addWidget(up); lay.addWidget(dn); lay.addWidget(rm)

    def _finite_limits(self):
        """The parameter's limits, with infinities replaced by a usable span.

        A module that advertises no min/max gives (-inf, +inf). Qt accepts an
        infinite spin range, but any DEFAULT computed from it is infinite too,
        and `np.linspace(0, inf, 21)` is not a scan -- it is a column of inf
        that used to sail through validation, because inf > inf is False.
        """
        lo, hi = self.param.limits
        lo = float(lo) if math.isfinite(lo) else -_UNBOUNDED
        hi = float(hi) if math.isfinite(hi) else _UNBOUNDED
        return lo, hi

    def _default_span(self, lo, hi):
        """A sensible finite from/to: start at 0 if it is in range, span 30%.

        An INT parameter spans its WHOLE range instead: an index axis is
        normally swept end to end, and 30 % of 0..19 is a strange default.
        """
        if getattr(self.param, "integer", False):
            return float(round(lo)), float(round(hi))
        start = 0.0 if lo <= 0 <= hi else lo
        stop = min(hi, start + (hi - lo) * 0.3)
        return start, stop

    def _sync_limits_label(self):
        lo, hi = self.param.limits
        unit = f" {self.param.unit}" if self.param.unit else ""
        # The service goes first: with several modules connected, "Position X"
        # alone does not say whose stage it is.
        module = split_id(self.param.id)[0]
        owner = f"{module} · " if module else ""
        if math.isfinite(lo) and math.isfinite(hi):
            self.limits_lbl.setText(f"{owner}{lo:g} to {hi:g}{unit}")
        else:
            self.limits_lbl.setText(f"{owner}no limits advertised")

    def refresh_limits(self):
        """Re-read the parameter's limits and re-clamp the boxes to them.

        LIMITS ARE NOT STATIC. piezo's travel ceiling drops from 200 to 160 um
        the moment an axis goes closed-loop; kim's armed leash replaces the
        travel clamp entirely; clMag's field range IS the loaded calibration.
        A row built before any of that shows a range the instrument no longer
        has, and the operator finds out when the service silently clamps their
        setpoint. Re-reading costs nothing.
        """
        lo, hi = self._finite_limits()
        for box in (self.start, self.stop):
            box.blockSignals(True)
            box.setRange(lo, hi)          # Qt re-clamps the held value itself
            box.blockSignals(False)
        self._sync_limits_label()
        self._sync_int_points()           # the array may have grown or shrunk

    def _spin(self, lo, hi, val):
        s = QtWidgets.QDoubleSpinBox(); s.setRange(lo, hi)
        # whole numbers for an int parameter: "0.000 to 19.000" for an array
        # index invites exactly the fractional sweep that used to hang a scan
        s.setDecimals(0 if self.integer else 3)
        s.setSingleStep(1.0 if self.integer else 1.0)
        s.setValue(val); s.setFixedWidth(84)
        s.valueChanged.connect(lambda *_: self.changed.emit())
        if self.integer:
            s.valueChanged.connect(lambda *_: self._sync_int_points())
        return s

    def _sync_int_points(self):
        """Cap `pts` at the number of DISTINCT values an int axis can take.

        Asking for 21 points across indices 0..19 can only repeat or round
        values -- there is no 0.95th scan point. The cap makes that impossible
        rather than leaving it to be discovered mid-scan.
        """
        if not self.integer:
            return
        span = int(round(abs(self.stop.value() - self.start.value()))) + 1
        self.num.blockSignals(True)
        self.num.setRange(1, max(1, span))
        if self.num.value() > span:
            self.num.setValue(span)
        self.num.blockSignals(False)
        self.num.setToolTip(
            f"{self.param.label} takes whole numbers only, so this sweep has at "
            f"most {span} points")

    #: Pixels of indent per nesting level, and how many levels get one. Loop
    #: depth is the thing an operator misreads most often -- "which of these is
    #: the slow one?" -- and a number in a column is easy to skim past, while a
    #: staircase is not. Capped: at five axes an uncapped indent would push the
    #: spin boxes off the card.
    INDENT_PX = 16
    INDENT_MAX = 4

    def refresh_level(self):
        """Show the loop depth: the number, and the row's own indentation."""
        level = self._level_getter(self)
        self.level_lbl.setText(str(level))
        lay = self.layout()
        _, top, right, bottom = lay.getContentsMargins()
        lay.setContentsMargins(10 + self.INDENT_PX * min(level, self.INDENT_MAX),
                               top, right, bottom)
        self.setToolTip(f"loop level {level} — "
                        + ("the OUTERMOST (slowest) axis" if level == 0
                           else "inside " + ", ".join(
                               r.param.label for r in self._siblings()[:level]))
                        + "\ndouble-click to preview the points it will visit")

    def mouseDoubleClickEvent(self, ev):
        # Only double-clicks on the row itself (name, labels, background) land
        # here: the spin boxes take their own, so editing a number is unaffected.
        self.preview.emit(self)
        ev.accept()

    def _siblings(self) -> list:
        """The rows around this one, outer first (for the tooltip)."""
        getter = getattr(self._level_getter, "__self__", None)
        return list(getattr(getter, "rows", []) or [])

    def to_axis(self) -> dict:
        if self.raw is not None:                 # loaded raster/zip: pass through
            return self.raw
        return {"type": "linear", "param": self.param.id,
                "start": self.start.value(), "stop": self.stop.value(),
                "num": self.num.value()}


# ──────────────────────────── one condition row ───────────────────────────────

class FixedRow(QtWidgets.QFrame):
    """A parameter held at ONE value for the whole scan: a measurement condition.

    The RF power a map was taken at, the field a frequency sweep sat in, the
    wavelength, the objective. Not an axis -- it never moves -- but every bit as
    much part of what the measurement IS, and the first thing you want to know
    when you open the file six months later.

    It is set once, before the first point (`Recipe.fixed`, applied by the
    engine), and it travels inside the saved definition and inside the
    measurement file, so loading either brings the conditions back with it.
    """

    changed = QtCore.Signal()
    remove = QtCore.Signal(object)

    def __init__(self, param, value: float | None = None):
        super().__init__()
        self.param = param
        self.setObjectName("axis")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4); lay.setSpacing(8)

        dot = QtWidgets.QLabel("=")
        dot.setStyleSheet(f"color:{C['muted']}; font-weight:800;")
        dot.setFixedWidth(16)
        lay.addWidget(dot)

        namebox = QtWidgets.QVBoxLayout(); namebox.setSpacing(0)
        name = QtWidgets.QLabel(param.label)
        name.setStyleSheet("font-weight:700;"); name.setFixedWidth(164)
        name.setToolTip(param.id)
        namebox.addWidget(name)
        self.limits_lbl = QtWidgets.QLabel()
        self.limits_lbl.setStyleSheet(f"color:{C['muted']}; font-size:10px;")
        self.limits_lbl.setFixedWidth(164)
        namebox.addWidget(self.limits_lbl)
        lay.addLayout(namebox)

        unit = QtWidgets.QLabel(f"[{param.unit}]")
        unit.setStyleSheet(f"color:{C['muted']};"); unit.setFixedWidth(44)
        lay.addWidget(unit)

        self.integer = bool(getattr(param, "integer", False))
        lo, hi = self._finite_limits()
        self.value_box = QtWidgets.QDoubleSpinBox()
        self.value_box.setRange(lo, hi)
        self.value_box.setDecimals(0 if self.integer else 3)
        self.value_box.setFixedWidth(104)
        start = value if value is not None else self._default_value(lo, hi)
        self.value_box.setValue(float(start))
        self.value_box.valueChanged.connect(lambda *_: self.changed.emit())
        lay.addWidget(self.value_box)

        lay.addStretch(1)
        rm = QtWidgets.QPushButton("✕"); rm.setObjectName("danger"); rm.setFixedWidth(30)
        rm.clicked.connect(lambda: self.remove.emit(self))
        lay.addWidget(rm)
        self._sync_limits_label()

    # the limit handling is the axis row's, minus the sweep
    _finite_limits = AxisRow._finite_limits
    _sync_limits_label = AxisRow._sync_limits_label

    @staticmethod
    def _default_value(lo, hi):
        """The parameter's CURRENT value would be ideal, but reading it here
        would block the GUI on a slow instrument. 0 if it is allowed, else the
        lower limit -- both are visibly a starting point to type over."""
        return 0.0 if lo <= 0 <= hi else lo

    def refresh_limits(self):
        lo, hi = self._finite_limits()
        self.value_box.blockSignals(True)
        self.value_box.setRange(lo, hi)
        self.value_box.blockSignals(False)
        self._sync_limits_label()

    def value(self) -> float:
        v = self.value_box.value()
        return int(round(v)) if self.integer else v


# ─────────────────────────────── routines ─────────────────────────────────────

#: The two moments the ROUTINES card edits, in the order they happen.
ROUTINE_MOMENTS = (("before_scan", "BEFORE SCAN"), ("after_scan", "AFTER SCAN"))

#: The label of the "no action" entry in a routine's action combo.
NO_ACTION = "(none)"


class RoutineSection(QtWidgets.QFrame):
    """One routine: parameters to set (in order), then ONE action to run.

    It is stored in the recipe as a `call` hook
    ({when: before_scan, action: call, args: {set: {...}, action: ...}}), so a
    routine is just data like the rest of the scan -- saved in the .yaml, carried
    inside every .nc, restored on load.

    The rows are FixedRows (parameter = value, with the live limits under the
    name), because a routine's set IS a setpoint, held for a moment instead of
    for the whole scan; showing it any differently would suggest it is checked
    differently, and it is not.

    The layout reads top to bottom in the order things happen: the sets, then
    "then run <action>". That is also the order the engine executes them in.
    """

    changed = QtCore.Signal()

    def __init__(self, when: str, title: str):
        super().__init__()
        self.when = when
        self.rows: list[FixedRow] = []
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)

        head = QtWidgets.QHBoxLayout()
        tag = QtWidgets.QLabel(title); tag.setObjectName("tag")
        head.addWidget(tag)
        self.title_lbl = tag
        self.empty_lbl = QtWidgets.QLabel("nothing -- set parameters, then run an action")
        self.empty_lbl.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
        self.empty_lbl.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                     QtWidgets.QSizePolicy.Preferred)
        head.addWidget(self.empty_lbl, 1)
        head.addStretch(1)
        v.addLayout(head)

        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(52)
        holder = QtWidgets.QWidget(); holder.setObjectName("root")
        self.lay = QtWidgets.QVBoxLayout(holder)
        self.lay.setContentsMargins(0, 0, 0, 0); self.lay.setSpacing(4)
        self.lay.addStretch(1)
        scroll.setWidget(holder)
        v.addWidget(scroll, 1)
        self.rows_scroll = scroll

        act = QtWidgets.QHBoxLayout()
        then = QtWidgets.QLabel("then run")
        then.setStyleSheet(f"color:{C['muted']};")
        act.addWidget(then)
        self.action_combo = QtWidgets.QComboBox()
        self.action_combo.setMinimumWidth(220)
        self.action_combo.currentIndexChanged.connect(lambda *_: self._changed())
        act.addWidget(self.action_combo)
        act.addStretch(1)
        v.addLayout(act)
        self.set_actions([])

    # ---- actions ----------------------------------------------------------

    def set_actions(self, actions) -> None:
        """Refill the combo from `registry.actions()`, keeping the choice if it
        is still offered."""
        keep = self.action_id()
        self.action_combo.blockSignals(True)
        self.action_combo.clear()
        self.action_combo.addItem(NO_ACTION, None)
        for a in actions:
            self.action_combo.addItem(f"{a.label}   ({a.id})", a.id)
            i = self.action_combo.count() - 1
            self.action_combo.setItemData(i, a.help or a.id, QtCore.Qt.ToolTipRole)
        i = self.action_combo.findData(keep) if keep else 0
        self.action_combo.setCurrentIndex(max(0, i))
        # Disabled, with the reason, rather than a combo offering only "(none)"
        # and leaving the operator to guess why the reference is not there.
        self.action_combo.setEnabled(bool(actions))
        self.action_combo.setToolTip(
            "The action runs AFTER every parameter above has been set and has "
            "settled, and the scan waits until it has finished."
            if actions else
            "No connected module offers an action a scan can wait on.\n"
            "(A module offers one by giving the action a `wait` block in describe.)")
        self.action_combo.blockSignals(False)
        self._changed()

    def action_id(self) -> str | None:
        return self.action_combo.currentData() if self.action_combo.count() else None

    def set_action(self, aid: str | None) -> bool:
        """Select `aid` (None = no action). False if the registry lacks it."""
        i = 0 if not aid else self.action_combo.findData(aid)
        if i < 0:
            return False
        self.action_combo.setCurrentIndex(i)
        return True

    # ---- sets ---------------------------------------------------------------

    def add_set(self, param, value: float | None = None) -> FixedRow:
        # One row per parameter, as with conditions: two rows would send two
        # setpoints, and only the last one would be the one the routine means.
        for row in self.rows:
            if row.param.id == param.id:
                if value is not None:
                    row.value_box.setValue(float(value))
                row.value_box.setFocus()
                return row
        row = FixedRow(param, value)
        row.changed.connect(self._changed)
        row.remove.connect(self.remove_set)
        self.rows.append(row)
        self.lay.insertWidget(self.lay.count() - 1, row)      # before the stretch
        self._changed()
        return row

    def remove_set(self, row) -> None:
        if row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            self._changed()

    def clear(self) -> None:
        for row in list(self.rows):
            self.remove_set(row)
        self.set_action(None)

    def _changed(self):
        if not hasattr(self, "action_combo"):     # still being built
            return
        self.empty_lbl.setVisible(not self.rows and not self.action_id())
        self.changed.emit()

    # ---- recipe --------------------------------------------------------------

    def to_hook(self) -> dict | None:
        """The `call` hook this section stands for, or None when it is empty
        (an empty routine is simply not written into the recipe)."""
        args = {}
        if self.rows:
            args["set"] = {r.param.id: r.value() for r in self.rows}
        if self.action_id():
            args["action"] = self.action_id()
        if not args:
            return None
        return {"when": self.when, "action": "call", "args": args}

    def describe(self) -> str:
        """One line for the summary: "field = 190 mT, then vna_reference"."""
        parts = [f"{r.param.label} = {r.value():g}"
                 + (f" {r.param.unit}" if r.param.unit else "") for r in self.rows]
        text = ", ".join(parts)
        if self.action_id():
            text = (text + ", then " if text else "") + self.action_id()
        return text


#: The triggers a THROUGHOUT routine offers: (label, when, edge).
THROUGHOUT_TRIGGERS = (("start of each sweep of", "each_sweep", "start"),
                       ("end of each sweep of", "each_sweep", "end"),
                       ("every N points", "every_n_points", None))

#: Hook keys a ThroughoutSection can show; a hook with any other key is kept
#: verbatim instead (see ScanBuilder._load_hooks).
THROUGHOUT_KEYS = {"when", "axis", "edge", "every", "n", "on_error", "action", "args"}


class ThroughoutSection(RoutineSection):
    """A routine that runs DURING the scan: every N points, or once per sweep.

    Same body as before/after (sets, then one action, both waited for), plus a
    TRIGGER line. "Each sweep of y" = once every time y starts again from its
    first value, i.e. whenever an axis outside y steps (hooks.py explains why
    that and not "y changed"). The axis list is the scan's DIMS, so a raster
    offers its x and y separately; it follows the axis stack as it is edited.

    Autofocus once per row is the case that asked for it, which is why "carry
    on if it fails" is ticked by default: a focus search that finds no peak
    should leave the focus where it was and let the map go on, not end it.
    """

    remove = QtCore.Signal(object)
    activated = QtCore.Signal(object)

    def __init__(self):
        super().__init__("each_sweep", "THROUGHOUT")
        self.setObjectName("routine")
        self._dims: list[str] = []
        self._count = None
        # No title of its own (the column has one), and no empty set area: a
        # routine that only runs an action -- autofocus -- is the usual case,
        # and several of them must fit in the card.
        self.title_lbl.hide()
        self.empty_lbl.setText("")
        self.rows_scroll.setMinimumHeight(0)

        # line 1: WHEN
        trig = QtWidgets.QHBoxLayout(); trig.setSpacing(6)
        self.trigger_combo = QtWidgets.QComboBox()
        for label, when, edge in THROUGHOUT_TRIGGERS:
            self.trigger_combo.addItem(label, (when, edge))
        trig.addWidget(self.trigger_combo)
        self.axis_combo = QtWidgets.QComboBox(); self.axis_combo.setMinimumWidth(90)
        trig.addWidget(self.axis_combo, 1)
        self.n_spin = QtWidgets.QSpinBox(); self.n_spin.setRange(1, 10_000_000)
        self.n_spin.setValue(100); self.n_spin.setFixedWidth(110)
        self.n_spin.setPrefix("N = ")
        trig.addWidget(self.n_spin)
        self.n_fill = QtWidgets.QWidget()
        trig.addWidget(self.n_fill, 1)
        rm = QtWidgets.QPushButton("✕"); rm.setObjectName("danger"); rm.setFixedWidth(30)
        rm.setToolTip("Remove this routine")
        rm.clicked.connect(lambda: self.remove.emit(self))
        trig.addWidget(rm)
        self.layout().insertLayout(1, trig)

        # line 3 (after "then run"): how often, what it costs, what if it fails
        foot = QtWidgets.QHBoxLayout(); foot.setSpacing(6)
        self.every_lbl = QtWidgets.QLabel("only every")
        self.every_lbl.setStyleSheet(f"color:{C['muted']};")
        foot.addWidget(self.every_lbl)
        self.every_spin = QtWidgets.QSpinBox(); self.every_spin.setRange(1, 100000)
        self.every_spin.setFixedWidth(56)
        self.every_spin.setToolTip("1 = every sweep; 3 = the 1st, 4th, 7th ... sweep")
        foot.addWidget(self.every_spin)
        self.every_unit = QtWidgets.QLabel("sweep")
        self.every_unit.setStyleSheet(f"color:{C['muted']};")
        foot.addWidget(self.every_unit)
        self.count_lbl = QtWidgets.QLabel()
        self.count_lbl.setStyleSheet(f"color:{C['accent']}; font-weight:700;")
        foot.addWidget(self.count_lbl)
        foot.addStretch(1)
        self.carry_on = QtWidgets.QCheckBox("carry on if it fails")
        self.carry_on.setChecked(True)
        self.carry_on.setToolTip(
            "Ticked: a failure (an autofocus that finds no peak, a timeout) is\n"
            "written to the log and the scan goes on -- the focus stays where\n"
            "it was. Unticked: the scan stops there, as for any other error.\n"
            "Abort always stops.")
        foot.addWidget(self.carry_on)
        self.layout().addLayout(foot)

        for w in (self.trigger_combo, self.axis_combo):
            w.currentIndexChanged.connect(lambda *_: self._changed())
        for w in (self.every_spin, self.n_spin):
            w.valueChanged.connect(lambda *_: self._changed())
        self.carry_on.toggled.connect(lambda *_: self._changed())
        self._sync_trigger_widgets()

    def mousePressEvent(self, ev):
        self.activated.emit(self)
        super().mousePressEvent(ev)

    # ---- trigger --------------------------------------------------------------

    def _trigger(self):
        return self.trigger_combo.currentData() or ("each_sweep", "start")

    def _sync_trigger_widgets(self):
        sweep = self._trigger()[0] == "each_sweep"
        for w in (self.axis_combo, self.every_lbl, self.every_spin, self.every_unit):
            w.setVisible(sweep)
        self.n_spin.setVisible(not sweep)
        self.n_fill.setVisible(not sweep)
        self.every_unit.setText("sweep  ·" if self.every_spin.value() == 1 else "sweeps  ·")
        # The set rows only take room when there are some.
        self.rows_scroll.setVisible(bool(self.rows))
        self.rows_scroll.setFixedHeight(min(3, len(self.rows)) * 46 + 4 if self.rows else 0)

    def _changed(self):
        if hasattr(self, "trigger_combo"):          # not during the base __init__
            self._sync_trigger_widgets()
        self.activated.emit(self)
        super()._changed()

    def set_dims(self, names, labels: dict | None = None) -> None:
        """Offer the scan's dims (outer -> inner). Keeps the choice; a choice no
        longer in the stack stays, marked, so validation names it instead of
        the routine silently jumping to another axis."""
        names = list(names)
        labels = labels or {}
        keep = self.axis_combo.currentData()
        if names == self._dims and self.axis_combo.count():
            return
        self._dims = names
        self.axis_combo.blockSignals(True)
        self.axis_combo.clear()
        for k, name in enumerate(names):
            depth = "outermost" if k == 0 else ("innermost" if k == len(names) - 1 else f"level {k}")
            self.axis_combo.addItem(labels.get(name) or name, name)
            self.axis_combo.setItemData(k, f"{name}  ({depth})", QtCore.Qt.ToolTipRole)
        if keep and keep not in names:
            self.axis_combo.addItem(f"{keep}  (not in the scan)", keep)
        if keep:
            self.axis_combo.setCurrentIndex(self.axis_combo.findData(keep))
        elif names:
            # A new routine: the INNERMOST axis, i.e. once per row -- the
            # autofocus case.
            self.axis_combo.setCurrentIndex(len(names) - 1)
        self.axis_combo.blockSignals(False)

    def set_trigger(self, when: str, axis=None, edge=None, every=1, n=None,
                    on_error="continue") -> None:
        for i, (_, w, e) in enumerate(THROUGHOUT_TRIGGERS):
            if w == when and (w != "each_sweep" or e == (edge or "start")):
                self.trigger_combo.setCurrentIndex(i)
        if axis is not None:
            if self.axis_combo.findData(axis) < 0:
                self.axis_combo.addItem(f"{axis}  (not in the scan)", axis)
            self.axis_combo.setCurrentIndex(self.axis_combo.findData(axis))
        self.every_spin.setValue(int(every or 1))
        if n:
            self.n_spin.setValue(int(n))
        self.carry_on.setChecked(on_error == "continue")

    def set_count(self, n) -> None:
        """How often it will fire, from hooks.firings(); None = cannot say."""
        self._count = n
        if n is None:
            self.count_lbl.setText("")
        else:
            self.count_lbl.setText(f"fires {n:,}×" if n else "never fires in this scan")

    # ---- recipe ----------------------------------------------------------------

    def to_hook(self) -> dict | None:
        hook = super().to_hook()
        if hook is None:
            return None
        when, edge = self._trigger()
        out = {"when": when}
        if when == "each_sweep":
            out.update(axis=self.axis_combo.currentData(), edge=edge,
                       every=self.every_spin.value())
        else:
            out["n"] = self.n_spin.value()
        out["on_error"] = "continue" if self.carry_on.isChecked() else "stop"
        out.update(action="call", args=hook["args"])
        return out

    def trigger_text(self) -> str:
        from scan_core.hooks import describe_trigger
        hook = self.to_hook()
        return describe_trigger(hook) if hook else ""


class RoutinesCard(QtWidgets.QFrame):
    """BEFORE / AFTER / THROUGHOUT, side by side when there is room.

    Side by side in the suite's wide Scan tab, stacked in the standalone
    builder's narrow middle column. "Room" is MEASURED -- the columns' own
    minimum widths, which grow when a routine gets parameter rows (a FixedRow
    needs ~430 px) -- not a fixed pixel count.

    The card sets an explicit small minimum width on purpose. Otherwise Qt
    makes the side-by-side layout's minimum (the SUM of the columns) the
    card's minimum, the window can then never be narrower than that, and the
    card never gets the chance to stack: the standalone builder opened 1955 px
    wide on 2026-09-25, wider than it was asked to be.
    """

    #: Heights (min, max) for the two arrangements: stacked, three routines
    #: need more room than side by side.
    WIDE_HEIGHT = (206, 400)
    STACKED_HEIGHT = (330, 380)

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.setMinimumWidth(240)
        self.box = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.TopToBottom)
        self.box.setSpacing(16)
        self._apply_height(False)

    def _needed_width(self) -> int:
        widths = [self.box.itemAt(i).widget().minimumSizeHint().width()
                  for i in range(self.box.count()) if self.box.itemAt(i).widget()]
        m = self.layout().contentsMargins() if self.layout() else QtCore.QMargins()
        return sum(widths) + self.box.spacing() * max(0, len(widths) - 1)             + m.left() + m.right()

    def _apply_height(self, wide: bool):
        lo, hi = self.WIDE_HEIGHT if wide else self.STACKED_HEIGHT
        self.setMinimumHeight(lo); self.setMaximumHeight(hi)

    def resizeEvent(self, event):
        wide = self.width() >= self._needed_width()
        want = (QtWidgets.QBoxLayout.LeftToRight if wide
                else QtWidgets.QBoxLayout.TopToBottom)
        if self.box.direction() != want:
            self.box.setDirection(want)
            self._apply_height(wide)
        super().resizeEvent(event)


# ───────────────────────────── axis point preview ─────────────────────────────

class AxisPreviewDialog(QtWidgets.QDialog):
    """Every setpoint one axis will send, as a table and a plot.

    Opened by double-clicking an axis row. It stays open and FOLLOWS the row:
    change from / to / pts and the list updates, so "what does 7 points from -3
    to 3 give me" is answered by looking rather than by arithmetic. The values
    come from `scan_core.preview`, i.e. the engine's own compile step plus the
    clamp and int rounding the instrument path applies -- so a point that will
    be clamped or rounded is shown as such, not as the number that was typed.
    """

    def __init__(self, row: AxisRow, registry, parent=None):
        super().__init__(parent)
        self.row, self.registry = row, registry
        self.setWindowTitle(f"Scan points — {row.param.label}")
        self.resize(560, 520)
        lay = QtWidgets.QVBoxLayout(self)

        self.header = QtWidgets.QLabel(); self.header.setWordWrap(True)
        self.header.setStyleSheet("font-weight:700;")
        lay.addWidget(self.header)
        self.warn = QtWidgets.QLabel(); self.warn.setWordWrap(True)
        self.warn.setStyleSheet(f"color:{C['danger']};")
        lay.addWidget(self.warn)

        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "point #")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setMinimumHeight(160)
        lay.addWidget(self.plot, 2)

        self.table = QtWidgets.QTableWidget()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)   # a list, not a form
        lay.addWidget(self.table, 3)

        buttons = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("Copy values")
        copy_btn.setToolTip("one line per point, tab-separated -- pastes into Excel/Origin")
        copy_btn.clicked.connect(self._copy)
        close_btn = QtWidgets.QPushButton("Close"); close_btn.clicked.connect(self.close)
        buttons.addWidget(copy_btn); buttons.addStretch(1); buttons.addWidget(close_btn)
        lay.addLayout(buttons)

        row.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        try:
            self.dims = preview_axis(self.row.to_axis(), self.registry)
        except Exception as exc:            # a malformed loaded axis: say so, don't crash
            self.dims = []
            self.header.setText("cannot compile this axis")
            self.warn.setText(str(exc))
            self.table.clear(); self.plot.clear()
            return

        members = [m for d in self.dims for m in d.members]
        lines = []
        for d in self.dims:
            for m in d.members:
                unit = f" {m.unit}" if m.unit else ""
                lines.append(f"{m.label}: {d.size} points, {m.sent[0]:g} → "
                             f"{m.sent[-1]:g}{unit}, {step_summary(m.sent)}")
        self.header.setText("\n".join(lines))
        warns = []
        for m in members:
            if m.n_changed:
                warns.append(f"{m.n_changed} point(s) of {m.label} are not sent as "
                             "typed (see the note column)")
            if m.n_repeats:
                warns.append(f"{m.n_repeats} point(s) of {m.label} repeat a value "
                             "-- measured twice, not at a new place")
        self.warn.setText("\n".join(warns))
        self.warn.setVisible(bool(warns))

        # table: one row per point; a raster's two dims are listed one after the other
        heads = ["#"] + [f"{m.label} [{m.unit}]" if m.unit else m.label for m in members]
        has_notes = any(m.n_changed for m in members)
        if has_notes:
            heads.append("note")
        n = max((d.size for d in self.dims), default=0)
        self.table.clear()
        self.table.setColumnCount(len(heads)); self.table.setRowCount(n)
        self.table.setHorizontalHeaderLabels(heads)
        for i in range(n):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(str(i)))
            notes = []
            for c, m in enumerate(members, start=1):
                if i < len(m.sent):
                    item = QtWidgets.QTableWidgetItem(f"{m.sent[i]:.6g}")
                    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                    if m.notes[i]:
                        item.setForeground(QtGui.QColor(C["danger"]))
                        notes.append(m.notes[i])
                    self.table.setItem(i, c, item)
            if has_notes:
                self.table.setItem(i, len(heads) - 1,
                                   QtWidgets.QTableWidgetItem("; ".join(notes)))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

        # plot: value against point number -- spacing and direction at a glance
        self.plot.clear()
        colours = [C["accent"], C["ok"], C["accent_hi"], C["muted"]]
        for k, m in enumerate(members):
            col = colours[k % len(colours)]
            self.plot.plot(np.arange(len(m.sent)), m.sent,
                           pen=pg.mkPen(col, width=1), symbol="o", symbolSize=6,
                           symbolBrush=col, symbolPen=None)
        self.plot.setLabel("left", heads[1] if len(members) == 1 else "value")

    def values_text(self) -> str:
        members = [m for d in self.dims for m in d.members]
        n = max((len(m.sent) for m in members), default=0)
        out = ["\t".join(["#"] + [m.pid for m in members])]
        for i in range(n):
            out.append("\t".join([str(i)] + [f"{m.sent[i]:.12g}" if i < len(m.sent)
                                             else "" for m in members]))
        return "\n".join(out)

    def _copy(self):
        QtWidgets.QApplication.clipboard().setText(self.values_text())

    def closeEvent(self, ev):
        try:
            self.row.changed.disconnect(self.refresh)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(ev)


# ───────────────────────────── background runner ──────────────────────────────

class ScanWorker(QtCore.QThread):
    progress = QtCore.Signal(int, int, float)
    partial = QtCore.Signal(object)      # the dataset SO FAR, a few times a second
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    saved = QtCore.Signal(str, int, int)     # path, done, total (done == total: final)
    log = QtCore.Signal(str)                 # what the routines are doing

    #: Seconds between live redraws. Building the snapshot costs something, and
    #: a 400-point scan of settling points does not need 60 fps.
    LIVE_EVERY_S = 0.4

    #: Scans shorter than this are only saved at the end: a checkpoint of a
    #: 20-point scan costs more than re-running it.
    CHECKPOINT_ABOVE = 100

    def __init__(self, recipe, registry, save_path=None):
        super().__init__()
        self.recipe, self.registry = recipe, registry
        self.save_path = Path(save_path) if save_path else None
        self._abort = False
        self._last_live = 0.0
        self._checkpoint_every = 0          # set once the total is known
        #: How the run ended, for a QUEUE deciding what comes next:
        #: "done", "aborted" (go on with the next scan) or "error" (stop).
        self.outcome: str | None = None
        self.error = ""

    def _write(self, ds, done, total):
        """Write the dataset to `save_path` ATOMICALLY (temp file, then replace).

        A netCDF written in place is unreadable while it is being written, and a
        crash mid-write would take the finished points with it. Writing beside
        it and renaming means the file on disk is always a complete scan.
        """
        if self.save_path is None:
            return
        tmp = self.save_path.with_suffix(".writing.nc")
        try:
            self.save_path.parent.mkdir(parents=True, exist_ok=True)
            ds.to_netcdf(tmp)
            os.replace(tmp, self.save_path)
            self.saved.emit(str(self.save_path), done, total)
        except Exception as exc:            # a full disk must not kill the scan
            self.failed.emit(f"could not save to {self.save_path}: {exc}")

    def abort(self):
        self._abort = True

    def _live(self, done, total, snapshot):
        """Redraw, and checkpoint the file, as the scan goes.

        `snapshot` is a factory: the dataset is only BUILT when it is wanted,
        so the points in between cost nothing. One snapshot serves both the
        plot and the checkpoint when they fall on the same point.
        """
        if not self._checkpoint_every:
            # every 1/10 of a LONG scan; a short one is only saved at the end
            self._checkpoint_every = max(1, total // 10) if total > self.CHECKPOINT_ABOVE else 0
        now = time.monotonic()
        due_save = (self._checkpoint_every and done < total
                    and done % self._checkpoint_every == 0)
        due_draw = done >= total or now - self._last_live >= self.LIVE_EVERY_S
        if not (due_save or due_draw):
            return
        ds = snapshot()
        if due_draw:
            self._last_live = now
            self.partial.emit(ds)
        if due_save:
            self._write(ds, done, total)

    def run(self):
        try:
            ds = run(self.recipe, self.registry,
                     on_progress=lambda d, t, e: self.progress.emit(d, t, e),
                     should_abort=lambda: self._abort,
                     on_point=self._live,
                     on_log=self.log.emit,
                     created_iso="live",
                     data_path=self.save_path)
            n = int(ds.sizes and np.prod([ds.sizes[d] for d in ds.sizes]) or 0)
            self._write(ds, n, n)          # the finished scan, saved for good
            # Abort pressed BETWEEN points ends the engine normally, with the
            # measured part; it is still an abort.
            self.outcome = "aborted" if self._abort else "done"
            self.done.emit(ds)
        except RoutineError as exc:
            # The AFTER-scan routine failed, but every point was measured: save
            # and show the data, THEN report the routine. Losing a finished map
            # because "field -> 0" timed out would be the worse failure.
            if exc.dataset is not None:
                ds = exc.dataset
                n = int(ds.sizes and np.prod([ds.sizes[d] for d in ds.sizes]) or 0)
                self._write(ds, n, n)
                self.done.emit(ds)
            self.outcome, self.error = "error", str(exc)
            self.failed.emit(str(exc))
        except ScanAborted as exc:
            # Abort pressed while an instrument was still settling: a normal
            # stop, not a failure -- nothing went wrong with the hardware.
            self.outcome = "aborted"
            self.failed.emit(f"aborted ({exc})")
        except Exception as exc:                 # surface validation/compile errors
            self.outcome, self.error = "error", str(exc)
            self.failed.emit(str(exc))


# ──────────────────────────────── scan queue ──────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}m {seconds % 60:02d}s"


class QueueDialog(QtWidgets.QDialog):
    """Name, order and prune a QUEUE of scans before it runs.

    Opens when more than one definition is loaded at once (or a queue file).
    Every entry is validated against the CURRENT registry up front, and Run
    stays off while any is invalid: the third scan must not turn out to name a
    module that is not connected, hours after the first one started.

    Drag to reorder (or up/down), double-click or F2 to rename, Delete removes.
    """

    def __init__(self, entries, registry, per_point_s: float = 0.05, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scan queue")
        self.resize(640, 460)
        self.registry, self.per_point_s = registry, float(per_point_s)
        self.run_requested = False
        v = QtWidgets.QVBoxLayout(self)
        head = QtWidgets.QLabel(
            "Scans run top to bottom, each into its own file named after it. "
            "Abort skips the scan that is running and the next one starts; an "
            "error stops the queue.")
        head.setWordWrap(True); head.setStyleSheet(f"color:{C['muted']};")
        v.addWidget(head)

        mid = QtWidgets.QHBoxLayout()
        self.list = QtWidgets.QListWidget()
        self.list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.list.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked
                                  | QtWidgets.QAbstractItemView.EditKeyPressed)
        self.list.model().rowsMoved.connect(lambda *_: self._refresh())
        self.list.itemChanged.connect(self._renamed)
        self.list.currentRowChanged.connect(lambda *_: self._show_detail())
        mid.addWidget(self.list, 1)
        side = QtWidgets.QVBoxLayout()
        for text, fn, tip in (("↑", lambda: self._move(-1), "Earlier"),
                              ("↓", lambda: self._move(+1), "Later"),
                              ("✕", self._delete, "Remove from the queue")):
            b = QtWidgets.QPushButton(text); b.setFixedWidth(36); b.setToolTip(tip)
            if text == "✕":
                b.setObjectName("danger")
            b.clicked.connect(fn)
            side.addWidget(b)
        side.addStretch(1)
        mid.addLayout(side)
        v.addLayout(mid, 1)
        QtGui.QShortcut(QtGui.QKeySequence.Delete, self.list, activated=self._delete)

        self.detail = QtWidgets.QLabel(); self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color:{C['muted']};")
        v.addWidget(self.detail)
        self.total = QtWidgets.QLabel(); self.total.setStyleSheet("font-weight:700;")
        v.addWidget(self.total)

        btns = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save queue…"); save.clicked.connect(self._save)
        save.setToolTip("Write the queue -- names, order and the scan definitions\n"
                        "themselves -- to one .yaml. Load it again to re-run it.")
        btns.addWidget(save)
        btns.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.run_btn = QtWidgets.QPushButton("▶  Run queue"); self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._run)
        btns.addWidget(self.run_btn)
        v.addLayout(btns)

        for e in entries:
            it = QtWidgets.QListWidgetItem(e.name)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsEditable | QtCore.Qt.ItemIsDragEnabled)
            it.setData(QtCore.Qt.UserRole, e)
            self.list.addItem(it)
        self.list.setCurrentRow(0)
        self._refresh()

    # ---- the list ------------------------------------------------------------
    def entries(self) -> list:
        return [self.list.item(i).data(QtCore.Qt.UserRole) for i in range(self.list.count())]

    def _renamed(self, item):
        e = item.data(QtCore.Qt.UserRole)
        if e is not None and e.name != item.text().strip():
            e.name = item.text().strip()
            self._refresh()

    def _move(self, delta):
        i = self.list.currentRow(); j = i + delta
        if i < 0 or not 0 <= j < self.list.count():
            return
        self.list.blockSignals(True)
        it = self.list.takeItem(i); self.list.insertItem(j, it)
        self.list.blockSignals(False)
        self.list.setCurrentRow(j)
        self._refresh()

    def _delete(self):
        i = self.list.currentRow()
        if i >= 0:
            self.list.takeItem(i)
            self._refresh()

    def _refresh(self):
        """Re-validate every entry and recolour; recompute the totals."""
        self._problems = scan_queue.validate_queue(self.entries(), self.registry)
        self.list.blockSignals(True)
        n_total, bad = 0, 0
        for i, (e, errs) in enumerate(zip(self.entries(), self._problems)):
            it = self.list.item(i)
            n = scan_queue.n_points(e.recipe, self.registry)
            n_total += n
            it.setForeground(QtGui.QColor(C["danger"] if errs else C["text"]))
            it.setToolTip("\n".join(errs) if errs else (e.source or e.name))
            bad += bool(errs)
        self.list.blockSignals(False)
        n = self.list.count()
        text = (f"{n} scan{'s' * (n != 1)}  ·  {n_total:,} points  ·  "
                f"≈ {_fmt_duration(n_total * self.per_point_s)} "
                f"at {self.per_point_s:g} s/pt (routines not included)")
        if bad:
            text += f"  ·  {bad} cannot run -- fix or remove the red one{'s' * (bad != 1)}"
        self.total.setText(text)
        self.total.setStyleSheet(f"font-weight:700; color:{C['danger'] if bad else C['text']};")
        self.run_btn.setEnabled(n > 0 and not bad)
        self._show_detail()

    def _show_detail(self):
        i = self.list.currentRow()
        if not 0 <= i < self.list.count() or i >= len(getattr(self, "_problems", [])):
            self.detail.setText("")
            return
        e = self.list.item(i).data(QtCore.Qt.UserRole)
        try:
            comp = e.recipe.compile(self.registry)
            shape = " × ".join(f"{d.name} {d.size}" for d in comp.dims)
        except Exception:
            shape = "?"
        lines = [f"{shape}   ·   detectors: {', '.join(e.recipe.detectors) or 'none'}"]
        if e.source:
            lines.append(f"from {e.source}")
        lines += [f"✕ {m}" for m in self._problems[i]]
        self.detail.setText("\n".join(lines))

    # ---- buttons --------------------------------------------------------------
    def _save(self):
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save queue", "queue.yaml", "Scan queue (*.yaml)")
        if fn:
            scan_queue.save_queue_file(fn, self.entries())

    def _run(self):
        self.run_requested = True
        self.accept()


# ──────────────────────────────── main window ─────────────────────────────────

class ScanBuilder(QtWidgets.QMainWindow):
    """Define a scan, and (standalone) run it.

    `embedded=True` builds everything exactly as before but does NOT place the
    right-hand pane -- the one holding Run/Abort, the progress bar, the ETA and
    the result plot. It is left unparented as `self.right_pane` for a host to
    put somewhere else.

    That is how the measurement suite splits "define" from "run" across two tabs
    without forking this class: the suite moves the same widgets, driven by the
    same tested code, into its Measurement tab. Skipping their CONSTRUCTION
    instead would break `_rebuild_summary`, which reads `self.summary`,
    `self.detail` and `self.per_pt`.
    """

    def __init__(self, registry=None, embedded: bool = False):
        super().__init__()
        self.registry = registry or build_sim_registry()
        self.rows: list[AxisRow] = []
        self._previews: dict = {}          # AxisRow -> its open AxisPreviewDialog
        self.fixed_rows: list[FixedRow] = []
        self.worker: ScanWorker | None = None
        self.dataset = None
        self.embedded = embedded
        self.group_names: dict[str, str] = {}   # module prefix -> branch title
        #: Set by the suite to Lab.refresh_stale: re-reads `describe` from any
        #: service whose manifest moved and pushes the new LIMITS onto the
        #: Parameters this builder holds. Without it the ranges on screen are a
        #: snapshot from connect time, and a sweep can be defined past what the
        #: instrument will now accept (the service then clamps, silently).
        self.limits_refresher = None
        #: Where finished (and part-finished) scans are written without being
        #: asked. Set by the suite from its data directory; None = no autosave,
        #: which is what the standalone builder and the tests want.
        self.autosave_dir = None
        self.last_saved: Path | None = None
        #: Set by the suite to its log: routine messages ("before_scan: set
        #: mag2d.field = 150 mT ... done") land there, and in `run_log`.
        self.on_log = None
        self.run_log: list[str] = []
        #: The routines card, one section per moment (see _build_routines).
        self.routines: dict[str, RoutineSection] = {}
        #: Routines that run DURING the scan (every N points / each sweep), in
        #: the THROUGHOUT column. Any number; `_active_throughout` is the one
        #: "+ Throughout" adds the selected parameter to.
        self.throughout: list[ThroughoutSection] = []
        self._active_throughout: ThroughoutSection | None = None
        self._throughout_actions = []
        #: The QUEUE being run (list of scan_queue.QueueEntry), or None. See
        #: run_queue(): one worker per scan, the next started when the last
        #: one's thread has finished.
        self._queue: list | None = None
        self._queue_i = -1
        self._queue_stop = ""                  # why the queue stops early, if it does
        self.queue_results: list[tuple[str, str]] = []   # (name, outcome)
        #: The hooks of the LOADED definition, with the routines the card edits
        #: replaced by ("routine", when) placeholders. Hooks the UI does not
        #: model (a wait_ms before every point, a second routine at the same
        #: moment) are kept here and written back on save, in their own place --
        #: a definition must not lose part of itself by passing through the UI.
        self._hook_template: list = []

        self.setWindowTitle(suite_title("Scan Builder"))   # "TR-MOKE · Scan Builder"
        self.resize(1280, 820)
        root = QtWidgets.QWidget(); root.setObjectName("root"); self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root); outer.setContentsMargins(16, 14, 16, 14); outer.setSpacing(12)

        if not embedded:
            title = QtWidgets.QLabel(suite_title("Scan Builder").upper()); title.setObjectName("title")
            outer.addWidget(title)

        cols = QtWidgets.QHBoxLayout(); cols.setSpacing(12); outer.addLayout(cols, 1)
        cols.addWidget(self._build_palette(), 0)
        cols.addWidget(self._build_middle(), 1)
        self.right_pane = self._build_right()
        if not embedded:
            cols.addWidget(self.right_pane, 0)

        self._rebuild_summary()

    def set_registry(self, registry, group_names: dict | None = None):
        """Swap the registry (sim <-> lab) and rebuild what depends on it.

        `group_names` maps a module prefix to the title of its branch in the
        palette ({"pm16": "Power meter"}); without one the prefix itself is shown.

        Axis rows are dropped rather than remapped: their parameters belong to
        the old registry, and a row pointing at a Settable nothing will drive is
        worse than an empty stack.
        """
        self.registry = registry
        self.group_names = dict(group_names or {})
        for row in list(self.rows):
            self._remove_row(row)
        self.rows.clear()
        for row in list(self.fixed_rows):       # same reason: they point at the old registry
            self._remove_fixed(row)
        self.fixed_rows.clear()
        # Routines likewise: their rows hold Parameters of the old registry, and
        # an action id from the simulator means nothing to a live lab. The hooks
        # kept from a loaded file go too -- keeping invisible hooks from a
        # definition whose axes were just dropped would make a later, freshly
        # built scan run parts of an old one.
        for section in self.routines.values():
            section.clear()
        for section in list(self.throughout):
            self.remove_throughout(section)
        self._hook_template = []
        self._reload_palette()
        self._rebuild_summary()

    # ---- panels ----------------------------------------------------------
    def _build_palette(self) -> QtWidgets.QWidget:
        # 300, not 250: the palette is a tree now, and a service heading such
        # as "Camera coordinator  ·  camera   (4)" needs the room.
        card = QtWidgets.QFrame(); card.setObjectName("card"); card.setFixedWidth(300)
        v = QtWidgets.QVBoxLayout(card); v.setContentsMargins(12, 12, 12, 12); v.setSpacing(8)
        v.addWidget(self._tag("AXES  ·  add a sweep"))
        self.set_tree = self._make_tree()
        self.set_tree.itemDoubleClicked.connect(lambda it, _col: self._add_item(it))
        v.addWidget(self.set_tree, 1)
        add = QtWidgets.QPushButton("＋  Add as axis"); add.setObjectName("primary")
        add.clicked.connect(self._add_selected)
        v.addWidget(add)
        addc = QtWidgets.QPushButton("＝  Add as condition")
        addc.setToolTip("Hold this parameter at ONE value for the whole scan.\n"
                        "It is set once before the first point, and saved with the\n"
                        "definition — so the file says what the measurement was\n"
                        "taken under, not only what was swept.")
        addc.clicked.connect(self._add_selected_fixed)
        v.addWidget(addc)
        # Routines: the same selection, held for a MOMENT before or after the
        # scan instead of for the whole of it ("go to 150 mT for the reference").
        rrow = QtWidgets.QHBoxLayout(); rrow.setSpacing(6)
        for when, text in (("before_scan", "＋ Before"), ("after_scan", "＋ After")):
            b = QtWidgets.QPushButton(text)
            b.setToolTip(
                "Add this parameter to the " + when.replace("_", "-") + " ROUTINE.\n"
                "The routine sets its parameters (each waits until settled), then\n"
                "runs its action (e.g. take a VNA reference), "
                + ("then the scan starts.\nParameters the scan already holds are "
                   "put back before the first point."
                   if when == "before_scan" else
                   "after the last point --\nand also after Abort.")
            )
            b.clicked.connect(lambda _=False, w=when: self._add_selected_routine(w))
            rrow.addWidget(b)
        b = QtWidgets.QPushButton("＋ Throughout")
        b.setToolTip("Add this parameter to a routine that runs DURING the scan\n"
                     "(every N points, or once per sweep of an axis) -- the one\n"
                     "last clicked, or a new one. For a routine that only runs an\n"
                     "action (autofocus), use ＋ New in the THROUGHOUT column.")
        b.clicked.connect(self._add_selected_throughout)
        rrow.addWidget(b)
        v.addLayout(rrow)

        v.addSpacing(6)          # the detectors are a different list, not a fourth button
        v.addWidget(self._tag("DETECTORS  ·  record"))
        self.det_tree = self._make_tree()
        self.det_tree.itemChanged.connect(lambda *_: self._rebuild_summary())
        v.addWidget(self.det_tree, 1)
        self._reload_palette()
        return card

    @staticmethod
    def _make_tree() -> QtWidgets.QTreeWidget:
        tree = QtWidgets.QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setColumnCount(1)
        tree.setIndentation(14)
        tree.setUniformRowHeights(True)
        return tree

    def _group_item(self, tree, module: str, count: int) -> QtWidgets.QTreeWidgetItem:
        """A service branch. Not selectable and not checkable: it is a heading,
        and ticking a whole service would record its idn strings and flags too."""
        title = self.group_names.get(module) or module or SIM_GROUP
        if module and title != module:
            title = f"{title}  ·  {module}"
        item = QtWidgets.QTreeWidgetItem([f"{title}   ({count})"])
        item.setFlags(QtCore.Qt.ItemIsEnabled)
        item.setData(0, QtCore.Qt.UserRole, None)
        font = item.font(0); font.setBold(True); item.setFont(0, font)
        item.setToolTip(0, module or "simulated registry")
        tree.addTopLevelItem(item)
        return item

    @staticmethod
    def _param_text(p) -> str:
        return f"{p.label}   ({p.unit})" if p.unit else p.label

    def _reload_palette(self) -> None:
        """Refill the axis and detector trees from the current registry.

        Separate from building the widgets so switching between the simulated
        and the live registry replaces the CONTENTS rather than the panel.
        """
        self.set_tree.clear()
        for module, params in group_by_module(self.registry.settables()).items():
            group = self._group_item(self.set_tree, module, len(params))
            for p in params:
                it = QtWidgets.QTreeWidgetItem(group, [self._param_text(p)])
                it.setData(0, QtCore.Qt.UserRole, p.id)
                it.setToolTip(0, p.id)
            group.setExpanded(True)

        self.det_tree.blockSignals(True)     # refilling is not a user edit
        self.det_tree.clear()
        gettables = self.registry.gettables()
        # Tick one detector by default, or a fresh registry starts with a scan
        # that records nothing. Prefer the old sim default if it is present,
        # then the first SCAN-SAFE detector (one with an acquire step, such as
        # pm16.power) -- the first in the list was pm16.range, a setting.
        default = next((p.id for p in gettables if p.id == "lockin_r"), None) \
            or next((p.id for p in gettables if getattr(p, "acquire", None)), None) \
            or (gettables[0].id if gettables else None)
        for module, params in group_by_module(gettables).items():
            group = self._group_item(self.det_tree, module, len(params))
            for p in params:
                it = QtWidgets.QTreeWidgetItem(group, [self._param_text(p)])
                it.setData(0, QtCore.Qt.UserRole, p.id)
                it.setToolTip(0, p.id)
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(0, QtCore.Qt.Checked if p.id == default
                                 else QtCore.Qt.Unchecked)
            group.setExpanded(True)
        self.det_tree.blockSignals(False)
        if self.routines:                    # not built yet on the first call
            self._refresh_routine_actions()

    def _det_items(self) -> list[QtWidgets.QTreeWidgetItem]:
        """Every detector leaf, across all service branches."""
        out = []
        for g in range(self.det_tree.topLevelItemCount()):
            group = self.det_tree.topLevelItem(g)
            out.extend(group.child(i) for i in range(group.childCount()))
        return out

    def _build_stack(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame(); card.setObjectName("card")
        v = QtWidgets.QVBoxLayout(card); v.setContentsMargins(12, 12, 12, 12); v.setSpacing(8)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(self._tag("AXIS STACK  ·  outer → inner")); head.addStretch(1)
        self.hint = QtWidgets.QLabel("double-click a parameter, or select and Add")
        self.hint.setStyleSheet(f"color:{C['muted']};"); head.addWidget(self.hint)
        v.addLayout(head)

        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        holder = QtWidgets.QWidget(); holder.setObjectName("root")
        self.stack_lay = QtWidgets.QVBoxLayout(holder)
        self.stack_lay.setContentsMargins(0, 0, 0, 0); self.stack_lay.setSpacing(8)
        self.stack_lay.addStretch(1)
        scroll.setWidget(holder)
        v.addWidget(scroll, 1)
        return card

    def _build_middle(self) -> QtWidgets.QWidget:
        """The axis stack, with the conditions underneath it.

        Two cards, not two tabs: what is swept and what is held are one
        description of one measurement, and hiding half of it behind a tab is
        how a scan gets run at last week's RF power.
        """
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(12)
        v.addWidget(self._build_stack(), 1)
        v.addWidget(self._build_conditions(), 0)
        v.addWidget(self._build_routines(), 0)
        return page

    def _build_routines(self) -> QtWidgets.QWidget:
        """ROUTINES: what happens once before the scan and once after it.

        Below the conditions because it is read in the same breath: what is
        swept, what is held, and what is done around it. The VNA-FMR case is
        the reason it exists -- go to a far-off field, take a reference, sweep
        the field, then field -> 0.
        """
        card = RoutinesCard()          # sizes itself: side by side or stacked
        v = QtWidgets.QVBoxLayout(card); v.setContentsMargins(12, 10, 12, 10); v.setSpacing(6)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(self._tag("ROUTINES  ·  before, during and after the scan"))
        self.routine_hint = QtWidgets.QLabel("select a parameter, then ＋ Before / ＋ After / ＋ Throughout")
        self.routine_hint.setStyleSheet(f"color:{C['muted']};")
        # A hint may be CUT, never widen the window (see RoutinesCard).
        self.routine_hint.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                        QtWidgets.QSizePolicy.Preferred)
        self.routine_hint.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        head.addWidget(self.routine_hint, 1)
        v.addLayout(head)
        v.addLayout(card.box, 1)
        actions = self.registry.actions() if hasattr(self.registry, "actions") else []
        for when, title in ROUTINE_MOMENTS:
            section = RoutineSection(when, title)
            # Fill BEFORE connecting: the summary it would trigger reads the
            # right-hand pane, which is built after this column.
            section.set_actions(actions)
            section.changed.connect(self._rebuild_summary)
            self.routines[when] = section
            card.box.addWidget(section, 1)
        card.box.addWidget(self._build_throughout(actions), 1)
        return card

    def _build_throughout(self, actions) -> QtWidgets.QWidget:
        """THROUGHOUT: any number of routines that fire during the scan."""
        col = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(col); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        tag = QtWidgets.QLabel("THROUGHOUT SCAN"); tag.setObjectName("tag")
        head.addWidget(tag)
        self.throughout_empty = QtWidgets.QLabel("nothing -- e.g. autofocus once per row")
        self.throughout_empty.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
        self.throughout_empty.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                            QtWidgets.QSizePolicy.Preferred)
        head.addWidget(self.throughout_empty, 1)
        head.addStretch(1)
        new = QtWidgets.QPushButton("＋ New")
        new.setToolTip("A routine that runs during the scan: every N points, or\n"
                       "once per sweep of an axis (e.g. autofocus before every row).\n"
                       "The scan waits until it has finished.")
        new.clicked.connect(lambda: self.add_throughout())
        head.addWidget(new)
        v.addLayout(head)
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(52)
        holder = QtWidgets.QWidget(); holder.setObjectName("root")
        self.throughout_lay = QtWidgets.QVBoxLayout(holder)
        self.throughout_lay.setContentsMargins(0, 0, 0, 0); self.throughout_lay.setSpacing(10)
        self.throughout_lay.addStretch(1)
        scroll.setWidget(holder)
        v.addWidget(scroll, 1)
        self._throughout_actions = actions
        return col

    def add_throughout(self, hook: dict | None = None) -> ThroughoutSection:
        """A new THROUGHOUT routine; `hook` fills its trigger from a recipe."""
        section = ThroughoutSection()
        section.set_actions(self._throughout_actions)
        names = self._dim_names()
        section.set_dims(names, self._dim_labels(names))
        if hook:
            section.set_trigger(hook.get("when"), axis=hook.get("axis"),
                                edge=hook.get("edge"), every=hook.get("every", 1),
                                n=hook.get("n"),
                                on_error=hook.get("on_error") or "stop")
        section.changed.connect(self._rebuild_summary)
        section.remove.connect(self.remove_throughout)
        section.activated.connect(self._set_active_throughout)
        self.throughout.append(section)
        self.throughout_lay.insertWidget(self.throughout_lay.count() - 1, section)
        self._set_active_throughout(section)
        self._rebuild_summary()
        return section

    def remove_throughout(self, section) -> None:
        if section in self.throughout:
            self.throughout.remove(section)
            section.setParent(None)
            if self._active_throughout is section:
                self._active_throughout = None
                if self.throughout:
                    self._set_active_throughout(self.throughout[-1])
            self._rebuild_summary()

    def _set_active_throughout(self, section) -> None:
        if section is self._active_throughout and section.styleSheet():
            return
        self._active_throughout = section
        for s in self.throughout:
            # The one "+ Throughout" adds to, outlined so that is not a guess.
            s.setStyleSheet(f"QFrame#routine {{ border-left: 3px solid "
                            f"{C['accent'] if s is section else C['border']}; "
                            f"padding-left: 6px; }}")

    def _add_selected_throughout(self):
        it = self.set_tree.currentItem()
        pid = it.data(0, QtCore.Qt.UserRole) if it is not None else None
        if pid:
            self.add_throughout_set(pid)

    def add_throughout_set(self, pid: str, value: float | None = None,
                           section=None) -> FixedRow | None:
        """Set `pid` in a THROUGHOUT routine (`section`, else the active one,
        else a new one). None if the registry has no settable of that id."""
        p = self.registry.get(pid)
        if p is None or getattr(p, "kind", "") != "settable":
            return None
        section = section or self._active_throughout or self.add_throughout()
        return section.add_set(p, value)

    def _dim_labels(self, names) -> dict:
        """dim name -> the parameter's label, where the dim is named after one."""
        out = {}
        for name in names:
            p = self.registry.get(name)
            if p is not None:
                out[name] = p.label
        return out

    def _dim_names(self) -> list[str]:
        """The scan's dims (outer -> inner) as the axis stack stands now."""
        try:
            return [d.name for d in
                    Recipe(axes=[r.to_axis() for r in self.rows]).compile(self.registry).dims]
        except Exception:
            return []

    def _refresh_routine_actions(self):
        actions = self.registry.actions() if hasattr(self.registry, "actions") else []
        for section in self.routines.values():
            section.set_actions(actions)
        self._throughout_actions = actions
        for section in self.throughout:
            section.set_actions(actions)

    def _build_conditions(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame(); card.setObjectName("card")
        # Tall enough for three conditions without scrolling (a field, a power
        # and a wavelength is a normal set), capped so it never crowds out the
        # axis stack above it.
        card.setMinimumHeight(184); card.setMaximumHeight(260)
        v = QtWidgets.QVBoxLayout(card); v.setContentsMargins(12, 10, 12, 10); v.setSpacing(6)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(self._tag("CONDITIONS  ·  held at one value")); head.addStretch(1)
        self.cond_hint = QtWidgets.QLabel("select a parameter, then Add as condition")
        self.cond_hint.setStyleSheet(f"color:{C['muted']};")
        head.addWidget(self.cond_hint)
        v.addLayout(head)

        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        holder = QtWidgets.QWidget(); holder.setObjectName("root")
        self.fixed_lay = QtWidgets.QVBoxLayout(holder)
        self.fixed_lay.setContentsMargins(0, 0, 0, 0); self.fixed_lay.setSpacing(6)
        self.fixed_lay.addStretch(1)
        scroll.setWidget(holder)
        v.addWidget(scroll, 1)
        return card

    def _build_right(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame(); card.setObjectName("card")
        # Fixed width only makes sense as the third column of the standalone
        # builder. On the suite's Measurement tab this pane IS the page, so let
        # it expand rather than stranding it in a 560 px strip.
        if not getattr(self, "embedded", False):
            card.setFixedWidth(560)
        v = QtWidgets.QVBoxLayout(card); v.setContentsMargins(12, 12, 12, 12); v.setSpacing(8)

        v.addWidget(self._tag("SCAN"))
        self.summary = QtWidgets.QLabel("—"); self.summary.setObjectName("big")
        v.addWidget(self.summary)
        self.detail = QtWidgets.QLabel(""); self.detail.setStyleSheet(f"color:{C['muted']};")
        self.detail.setWordWrap(True); v.addWidget(self.detail)

        # The NAME of the measurement, here, before it runs -- not a default you
        # discover afterwards in the log. It goes into the file name and into the
        # scan definition the file carries.
        nrow = QtWidgets.QHBoxLayout()
        nrow.addWidget(QtWidgets.QLabel("name"))
        self.name_edit = QtWidgets.QLineEdit("scan")
        self.name_edit.setToolTip(
            "Goes into the file name: <data dir>\\<date>\\<time>_<name>.nc\n"
            "The time is always there, so repeating a scan never overwrites the\n"
            "one before it. Characters a file name cannot hold become '_'.")
        self.name_edit.setMaximumWidth(260)
        self.name_edit.textChanged.connect(lambda *_: self._refresh_save_target())
        nrow.addWidget(self.name_edit)
        nrow.addStretch(1)
        v.addLayout(nrow)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("per-point (s)"))
        self.per_pt = QtWidgets.QDoubleSpinBox(); self.per_pt.setRange(0.0, 100); self.per_pt.setDecimals(3)
        self.per_pt.setValue(0.05); self.per_pt.valueChanged.connect(lambda *_: self._rebuild_summary())
        row.addWidget(self.per_pt)
        self.zigzag_box = QtWidgets.QCheckBox("zig-zag")
        self.zigzag_box.setToolTip(
            "Sweep every other pass of the inner axis BACKWARDS, so the stage does not\n"
            "fly back to the start of each row. The data is identical -- only the path\n"
            "between points is shorter.\n\n"
            "Off by default: it is only safe where a point does not depend on the\n"
            "direction it was approached from. An open-loop slip-stick stage, backlash,\n"
            "or magnetic hysteresis all land somewhere slightly different coming back.")
        self.zigzag_box.toggled.connect(lambda *_: self._rebuild_summary())
        row.addWidget(self.zigzag_box)
        row.addStretch(1)
        self.run_btn = QtWidgets.QPushButton("▶  Run scan"); self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run_scan)
        self.abort_btn = QtWidgets.QPushButton("■ Abort"); self.abort_btn.setObjectName("danger")
        self.abort_btn.clicked.connect(self._abort); self.abort_btn.setEnabled(False)
        self.stop_queue_btn = QtWidgets.QPushButton("■■ Stop queue")
        self.stop_queue_btn.setObjectName("danger")
        self.stop_queue_btn.setToolTip("Abort the scan that is running AND every scan after it.\n"
                                       "(Abort alone skips to the next scan.)")
        self.stop_queue_btn.clicked.connect(self.stop_queue)
        self.stop_queue_btn.hide()
        row.addWidget(self.run_btn); row.addWidget(self.abort_btn); row.addWidget(self.stop_queue_btn)
        v.addLayout(row)

        # "Scan 2 of 5 · name" while a QUEUE runs; its summary when it ends.
        self.queue_lbl = QtWidgets.QLabel("")
        self.queue_lbl.setStyleSheet(f"color:{C['accent']}; font-weight:700;")
        self.queue_lbl.setWordWrap(True)
        self.queue_lbl.hide()
        v.addWidget(self.queue_lbl)

        self.progress = QtWidgets.QProgressBar(); self.progress.setValue(0)
        v.addWidget(self.progress)
        self.save_lbl = QtWidgets.QLabel(""); self.save_lbl.setObjectName("hint")
        self.save_lbl.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
        self.save_lbl.setWordWrap(True)
        v.addWidget(self.save_lbl)

        # Result: the general N-D viewer, not a fixed pair of axes. The same
        # widget serves the Data tab, so what you watch during a run behaves
        # exactly like what you open a saved file with.
        self.view = DataView()
        v.addWidget(self.view, 1)
        self.det_combo = self.view.det_combo       # kept: callers/tests use it
        self.plot, self.img, self.curve = self.view.plot, self.view.img, self.view.curve

        brow = QtWidgets.QHBoxLayout()
        load = QtWidgets.QPushButton("Load scan…"); load.clicked.connect(self._load_dialog)
        load.setToolTip("Fill the axis stack and detectors from a saved definition:\n"
                        "a .yaml recipe, or a measured .nc file (every measurement\n"
                        "carries the definition that produced it).\n\n"
                        "Select SEVERAL (or a saved queue) to run them one after\n"
                        "another: a dialog lets you name, order and prune them.")
        save = QtWidgets.QPushButton("Save scan…"); save.clicked.connect(self._save_dialog)
        save.setToolTip("Write the axis stack and ticked detectors to a .yaml recipe.")
        savd = QtWidgets.QPushButton("Save data (.nc)…"); savd.clicked.connect(self._save_data_dialog)
        brow.addWidget(load); brow.addWidget(save); brow.addStretch(1); brow.addWidget(savd)
        v.addLayout(brow)
        return card

    def _tag(self, t):
        l = QtWidgets.QLabel(t); l.setObjectName("tag"); return l

    # ---- axis stack management -------------------------------------------
    def _add_selected(self):
        self._add_item(self.set_tree.currentItem())

    def _add_item(self, it):
        """Add the parameter under a tree item as an axis; a service heading is ignored."""
        pid = it.data(0, QtCore.Qt.UserRole) if it is not None else None
        if pid:
            self.add_axis(pid)

    def add_axis(self, pid, raw=None):
        p = self.registry.get(pid)
        row = AxisRow(p, self._level_of)
        row.raw = raw
        row.changed.connect(self._rebuild_summary)
        row.remove.connect(self._remove_row)
        row.move.connect(self._move_row)
        row.preview.connect(self.preview_row)
        self.rows.append(row)
        self.stack_lay.insertWidget(self.stack_lay.count() - 1, row)  # before stretch
        self._relevel(); self._rebuild_summary()

    def _add_selected_fixed(self):
        it = self.set_tree.currentItem()
        pid = it.data(0, QtCore.Qt.UserRole) if it is not None else None
        if pid:
            self.add_fixed(pid)

    def add_fixed(self, pid, value: float | None = None) -> FixedRow | None:
        """Hold `pid` at one value for the whole scan.

        Adding the same parameter twice would send two setpoints and keep the
        last, so the second add just moves the cursor to the row that is already
        there -- the operator meant to change it, not to have two.
        """
        p = self.registry.get(pid)
        if p is None:
            return None
        for row in self.fixed_rows:
            if row.param.id == pid:
                if value is not None:
                    row.value_box.setValue(float(value))
                row.value_box.setFocus()
                return row
        row = FixedRow(p, value)
        row.changed.connect(self._rebuild_summary)
        row.remove.connect(self._remove_fixed)
        self.fixed_rows.append(row)
        self.fixed_lay.insertWidget(self.fixed_lay.count() - 1, row)   # before stretch
        self._rebuild_summary()
        return row

    def _add_selected_routine(self, when: str):
        it = self.set_tree.currentItem()
        pid = it.data(0, QtCore.Qt.UserRole) if it is not None else None
        if pid:
            self.add_routine_set(when, pid)

    def add_routine_set(self, when: str, pid: str,
                        value: float | None = None) -> FixedRow | None:
        """Set `pid` to `value` in the before_scan / after_scan routine.

        None if the registry has no SETTABLE of that id (the routine could not
        set it, so the row would be a promise nothing keeps).
        """
        p = self.registry.get(pid)
        if p is None or getattr(p, "kind", "") != "settable" or when not in self.routines:
            return None
        return self.routines[when].add_set(p, value)

    def set_routine_action(self, when: str, aid: str | None) -> bool:
        """Choose the action a routine runs (None = none). False if unknown."""
        return self.routines[when].set_action(aid)

    def has_definition(self) -> bool:
        """True when anything has been defined that a registry swap would drop."""
        return bool(self.rows or self.fixed_rows
                    or any(s.to_hook() for s in self.routines.values())
                    or any(s.to_hook() for s in self.throughout))

    def _remove_fixed(self, row):
        self.fixed_rows.remove(row); row.setParent(None)
        self._rebuild_summary()

    def preview_row(self, row) -> AxisPreviewDialog:
        """Show (or raise) the point list of one axis row; one window per row."""
        dlg = self._previews.get(row)
        if dlg is None:
            dlg = AxisPreviewDialog(row, self.registry, self)
            dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose)
            dlg.destroyed.connect(lambda *_, r=row: self._previews.pop(r, None))
            self._previews[row] = dlg
        dlg.show(); dlg.raise_(); dlg.activateWindow()
        return dlg

    def _remove_row(self, row):
        dlg = self._previews.pop(row, None)
        if dlg is not None:
            dlg.close()
        self.rows.remove(row); row.setParent(None)
        self._relevel(); self._rebuild_summary()

    def _move_row(self, row, delta):
        i = self.rows.index(row); j = i + delta
        if 0 <= j < len(self.rows):
            self.rows[i], self.rows[j] = self.rows[j], self.rows[i]
            for r in self.rows:
                self.stack_lay.removeWidget(r)
            for k, r in enumerate(self.rows):
                self.stack_lay.insertWidget(k, r)
            self._relevel(); self._rebuild_summary()

    def _level_of(self, row):
        return self.rows.index(row) if row in self.rows else 0

    def _relevel(self):
        for r in self.rows:
            r.refresh_level()

    # ---- recipe (build / load / save) ------------------------------------
    def build_recipe(self) -> Recipe:
        dets = [it.data(0, QtCore.Qt.UserRole) for it in self._det_items()
                if it.checkState(0) == QtCore.Qt.Checked]
        return Recipe(name=self.name_edit.text().strip() or "scan",
                      fixed={r.param.id: r.value() for r in self.fixed_rows},
                      axes=[r.to_axis() for r in self.rows],
                      detectors=dets,
                      hooks=self._compose_hooks(),
                      zigzag=self.zigzag_box.isChecked())

    def _compose_hooks(self) -> list[dict]:
        """The loaded hooks in their original order, with the card's routines
        written back where they came from (and new ones at the end)."""
        out, placed = [], set()
        for h in self._hook_template:
            if isinstance(h, tuple) and h[:1] == ("routine",):
                placed.add(h[1])
                hook = self.routines[h[1]].to_hook()
                if hook:
                    out.append(hook)
            elif isinstance(h, tuple) and h[:1] == ("throughout",):
                # A removed section is simply gone. Placed by identity: two
                # THROUGHOUT routines may share a trigger.
                if h[1] in self.throughout:
                    placed.add(id(h[1]))
                    hook = h[1].to_hook()
                    if hook:
                        out.append(hook)
            else:
                out.append(copy.deepcopy(h))
        for when, _ in ROUTINE_MOMENTS:
            if when not in placed:
                hook = self.routines[when].to_hook()
                if hook:
                    out.append(hook)
        for section in self.throughout:
            if id(section) not in placed:
                hook = section.to_hook()
                if hook:
                    out.append(hook)
        return out

    def _load_hooks(self, hooks) -> list[str]:
        """Fill the ROUTINES card from a recipe's hooks; return missing ids.

        The FIRST plain `call` hook at before_scan / after_scan is what the card
        shows. Anything else -- other actions, other moments, a second routine at
        the same moment, a call with arguments the card cannot show -- is kept
        verbatim and saved back unchanged.

        Missing parameters and actions are FLAGGED, in every call hook, exactly
        like a missing axis: a definition written against the lab must not come
        back on the simulator quietly skipping its reference.
        """
        missing: list[str] = []
        for section in self.routines.values():
            section.clear()
        for section in list(self.throughout):
            self.remove_throughout(section)
        self._hook_template = []
        taken = set()
        for h in hooks or []:
            if not isinstance(h, dict):
                self._hook_template.append(h)
                continue
            args = h.get("args") if isinstance(h.get("args"), dict) else None
            is_call = h.get("action") == "call" and args is not None
            if is_call:
                sets = args.get("set") or {}
                for pid in (sets if isinstance(sets, dict) else {}):
                    p = self.registry.get(pid)
                    if p is None or getattr(p, "kind", "") != "settable":
                        missing.append(pid)
                aid = args.get("action")
                get_action = getattr(self.registry, "get_action", None)
                if aid and (get_action is None or get_action(aid) is None):
                    missing.append(aid)
            when = h.get("when")
            if (is_call and when in ("each_sweep", "every_n_points")
                    and set(h) <= THROUGHOUT_KEYS
                    and set(args) <= {"set", "action"}
                    and isinstance(args.get("set") or {}, dict)):
                section = self.add_throughout(h)
                self._hook_template.append(("throughout", section))
                for pid, value in (args.get("set") or {}).items():
                    self.add_throughout_set(pid, float(value), section=section)
                if args.get("action"):
                    section.set_action(args["action"])
                continue
            modelled = (is_call and when in self.routines and when not in taken
                        and set(args) <= {"set", "action"}
                        and isinstance(args.get("set") or {}, dict))
            if not modelled:
                self._hook_template.append(copy.deepcopy(h))
                continue
            taken.add(when)
            self._hook_template.append(("routine", when))
            for pid, value in (args.get("set") or {}).items():
                self.add_routine_set(when, pid, float(value))
            if args.get("action"):
                self.set_routine_action(when, args["action"])
        return missing

    def load_recipe(self, recipe: Recipe) -> list[str]:
        """Rebuild the axis stack and detector ticks from `recipe`.

        Returns the ids the CURRENT registry does not have. A definition is
        written down against the instruments of the day; loading it next week
        with the lock-in switched off must not crash, and must not silently drop
        half the scan either -- so the missing ids come back for the caller to
        show.
        """
        missing = []
        self.zigzag_box.blockSignals(True)
        self.zigzag_box.setChecked(bool(getattr(recipe, "zigzag", False)))
        self.zigzag_box.blockSignals(False)
        if getattr(recipe, "name", ""):
            self.name_edit.setText(recipe.name)
        for r in list(self.rows):
            self._remove_row(r)
        for r in list(self.fixed_rows):
            self._remove_fixed(r)
        # The routines (and every other hook, kept for saving back).
        missing += self._load_hooks(getattr(recipe, "hooks", None) or [])
        # Conditions first: they are what the measurement was taken UNDER, and
        # a definition that comes back without them is a different measurement.
        for pid, value in (recipe.fixed or {}).items():
            if self.add_fixed(pid, value) is None:
                missing.append(pid)
        for ax in recipe.axes:
            pid = (ax.get("param") or ax.get("x", {}).get("param")
                   or (ax.get("members") or [{}])[0].get("param"))
            if not pid or self.registry.get(pid) is None:
                missing.append(pid or f"<{ax.get('type', 'axis')}>")
                continue
            if ax.get("type") == "linear":
                self.add_axis(pid)
                row = self.rows[-1]
                if "start" in ax: row.start.setValue(float(ax["start"]))
                if "stop" in ax: row.stop.setValue(float(ax["stop"]))
                if ax.get("num"): row.num.setValue(int(ax["num"]))
            else:
                # raster/zip/array: keep as a pass-through row on the first member
                self.add_axis(pid, raw=ax)
                self.rows[-1].hint = ax["type"]
        want = set(recipe.detectors)
        have = {it.data(0, QtCore.Qt.UserRole) for it in self._det_items()}
        missing += [d for d in recipe.detectors if d not in have]
        for it in self._det_items():
            it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole) in want
                             else QtCore.Qt.Unchecked)
        self._rebuild_summary()
        return missing

    @staticmethod
    def recipe_from_file(path: str) -> Recipe:
        """A scan definition from a .yaml recipe OR a measured .nc file.

        Every dataset the engine writes carries its recipe in `recipe_json`, so
        the measurement file IS a scan definition -- no need to keep a separate
        recipe next to the data and hope they stay in step.
        """
        return scan_queue.recipe_from_file(path)

    # ---- summary / validation --------------------------------------------
    def _rebuild_summary(self):
        # THROUGHOUT routines name an axis: give them the current stack FIRST,
        # so the recipe below is built from what they now offer.
        if not hasattr(self, "summary"):          # right pane not built yet
            return
        if self.queue_running():
            # The pane describes the scan that is RUNNING, not the editor's.
            return
        dim_names = self._dim_names()
        labels = self._dim_labels(dim_names)
        for section in self.throughout:
            section.set_dims(dim_names, labels)
        if hasattr(self, "throughout_empty"):
            self.throughout_empty.setVisible(not self.throughout)
        recipe = self.build_recipe()
        errs = recipe.validate(self.registry)
        conditions = ("   ·   " + ", ".join(
            f"{r.param.label} = {r.value():g}{(' ' + r.param.unit) if r.param.unit else ''}"
            for r in self.fixed_rows)) if self.fixed_rows else ""
        # The routines, one short clause each. Not in the ETA: a routine's time
        # is a magnet ramp or a reference sweep, which nothing here can predict.
        for when, _ in ROUTINE_MOMENTS:
            section = self.routines.get(when)
            text = section.describe() if section is not None else ""
            if text:
                conditions += f"   ·   {when.replace('_', ' ')}: {text}"
            if when == "before_scan":        # in the order things happen
                conditions += self._throughout_summary(recipe)
        if not self.rows:
            self.summary.setText("no axes")
            self.detail.setText(conditions.strip(" ·") if conditions else "")
            return
        if errs:
            self.summary.setText("invalid")
            self.summary.setStyleSheet(f"color:{C['danger']}; font-size:22px; font-weight:800;")
            self.detail.setText("• " + "\n• ".join(errs)); return
        self.summary.setStyleSheet("")
        comp = recipe.compile(self.registry)
        shape = "×".join(str(s) for s in comp.shape)
        n = comp.n_points
        eta = n * self.per_pt.value()
        self.summary.setText(f"{len(comp.dims)}-D   {shape} = {n:,} pts")
        self.detail.setText(f"dims: {', '.join(d.name for d in comp.dims)}   ·   "
                            f"ETA ≈ {int(eta // 60):d}m {int(eta % 60):02d}s "
                            f"@ {self.per_pt.value():g}s/pt"
                            + ("   ·   zig-zag" if self.zigzag_box.isChecked() else "")
                            + conditions)

    def _throughout_summary(self, recipe) -> str:
        """One clause per THROUGHOUT routine, with how often it fires -- the
        cost of "autofocus every row" is visible before Run. Not in the ETA:
        how long an autofocus takes is not something this window knows."""
        from scan_core.hooks import firings
        try:
            comp = recipe.compile(self.registry)
            shape, names = comp.shape, [d.name for d in comp.dims]
        except Exception:
            shape, names = (), []
        text = ""
        for section in self.throughout:
            hook = section.to_hook()
            n = firings(hook, shape, names) if (hook and shape) else None
            section.set_count(n)
            if hook:
                text += (f"   ·   {section.trigger_text()}: {section.describe()}"
                         + (f" ({n:,}×)" if n is not None else ""))
        return text

    def refresh_axis_limits(self) -> list:
        """Re-read the live limits and re-clamp every axis row to them.

        Called before a run and whenever the Scan tab is opened. Limits MOVE:
        the camera's scan array changes size, kim's leash replaces the travel
        clamp, piezo's ceiling drops on closed loop, clMag's range IS its
        calibration.
        """
        moved, problem = [], ""
        if self.limits_refresher is not None:
            try:
                moved = list(self.limits_refresher() or [])
            except Exception as exc:        # a dead service must not block the UI
                problem = f"could not refresh limits: {exc}"
        for row in self.rows:
            row.refresh_limits()
        for row in self.fixed_rows:         # a condition is a setpoint too
            row.refresh_limits()
        for section in [*self.routines.values(), *self.throughout]:   # ... and a routine's
            for row in section.rows:
                row.refresh_limits()
        self._rebuild_summary()             # rewrites `detail` ...
        if problem:
            self.detail.setText(problem)    # ... so say it AFTER, not before
        return moved

    # ---- run --------------------------------------------------------------
    def run_scan(self, *, block=False):
        # The bounds a recipe is validated against must be the CURRENT ones, or
        # a sweep the instrument no longer accepts passes validation and the
        # service quietly clamps every point past the end.
        self.refresh_axis_limits()
        recipe = self.build_recipe()
        errs = recipe.validate(self.registry)
        if errs or not self.rows:
            self._rebuild_summary(); return
        # One scan at a time. Starting a second while the first is still
        # unwinding puts TWO engines on the same instruments: they interleave
        # setpoints, and the run looks stuck for reasons nothing reports.
        if self.worker is not None and self.worker.isRunning():
            self.detail.setText("a scan is still running — press Abort and wait "
                                "for it to stop")
            return
        self.progress.setValue(0)
        self.run_log = []
        if block:                                   # synchronous path (tests/render)
            try:
                ds = run(recipe, self.registry, created_iso="live", on_log=self._on_log)
            except RoutineError as exc:
                if exc.dataset is not None:
                    self._on_done(exc.dataset)
                self._on_failed(str(exc))
                return
            self._on_done(ds); return

        # Check the SAVE before the scan, not after it. A scan is minutes to
        # hours; finding out at the end that the folder is read-only or the
        # share is gone means the measurement is only in memory, and one crash
        # from being nothing.
        ok, msg = self.check_save_target()
        if not ok and not self._confirm_unsaved(msg):
            return

        self._start_worker(recipe)

    def _start_worker(self, recipe) -> "ScanWorker":
        """Start one scan in its thread; the run pane follows it."""
        self.run_btn.setEnabled(False); self.abort_btn.setEnabled(True)
        path = self.autosave_path(recipe)
        self.worker = ScanWorker(recipe, self.registry, save_path=path)
        self.worker.progress.connect(self._on_progress)
        self.worker.partial.connect(self._on_partial)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.saved.connect(self._on_saved)
        self.worker.log.connect(self._on_log)
        self.save_lbl.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
        self.save_lbl.setText(f"saving to {path}" if path else
                              "not saving automatically (no data directory set)")
        self.worker.start()
        return self.worker

    # ---- queue --------------------------------------------------------------
    def queue_running(self) -> bool:
        return self._queue is not None

    def run_queue(self, entries) -> bool:
        """Run `entries` (scan_queue.QueueEntry) one after another.

        Checked as a WHOLE first -- live limits refreshed, every entry
        validated, the data directory probed -- so nothing starts that cannot
        finish. Returns False (and says why in the summary) if it did not start.
        The editor's own definition is not touched: the queue runs its snapshots.
        """
        if self.worker is not None and self.worker.isRunning() or self.queue_running():
            self.detail.setText("a scan is still running — press Abort and wait "
                                "for it to stop")
            return False
        entries = list(entries)
        self.refresh_axis_limits()
        problems = scan_queue.validate_queue(entries, self.registry)
        bad = [f"{e.name}: {'; '.join(errs)}" for e, errs in zip(entries, problems) if errs]
        if not entries or bad:
            self.detail.setText("queue NOT started -- " + (" | ".join(bad) or "it is empty"))
            return False
        ok, msg = self.check_save_target()
        if not ok and not self._confirm_unsaved(msg):
            return False
        self._queue, self._queue_i, self._queue_stop = entries, -1, ""
        self.queue_results = []
        self.run_log = []
        self.stop_queue_btn.show(); self.stop_queue_btn.setEnabled(True)
        self.queue_lbl.show()
        self._qlog(f"queue: {len(entries)} scans")
        self._queue_next()
        return True

    def stop_queue(self):
        """Abort the running scan and do not start another."""
        if self.queue_running():
            self._queue_stop = "stopped by the operator"
            self._abort()
            if self.worker is None:              # between two scans
                self._queue_next()

    def _qlog(self, msg: str):
        self.run_log.append(msg)
        if self.on_log is not None:
            self.on_log(msg)

    def _queue_label(self, eta_s: float | None = None):
        i, q = self._queue_i, self._queue
        if eta_s is None:
            try:
                comp = q[i].recipe.compile(self.registry)
                self.summary.setStyleSheet("")
                self.summary.setText(f"{len(comp.dims)}-D   "
                                     f"{'×'.join(str(n) for n in comp.shape)} = "
                                     f"{comp.n_points:,} pts")
                self.detail.setText(f"queue scan {i + 1} of {len(q)}: {q[i].name}   ·   "
                                    f"dims: {', '.join(d.name for d in comp.dims)}   ·   "
                                    f"editing the definition does not change the queue")
            except Exception:
                pass
        rest = sum(scan_queue.n_points(e.recipe, self.registry) for e in q[i + 1:])
        eta = (eta_s or 0.0) + rest * self.per_pt.value()
        self.queue_lbl.setText(f"Scan {i + 1} of {len(q)}  ·  {q[i].name}"
                               + (f"   ·   queue ≈ {_fmt_duration(eta)} left"
                                  if eta_s is not None else ""))

    def _queue_next(self):
        if self._queue is None:
            return
        self._queue_i += 1
        if self._queue_stop or self._queue_i >= len(self._queue):
            self._queue_end()
            return
        entry = self._queue[self._queue_i]
        self._queue_label()
        self._qlog(f"queue: scan {self._queue_i + 1} of {len(self._queue)} "
                   f"'{entry.name}' started")
        self.progress.setValue(0)
        worker = self._start_worker(entry.named_recipe())
        # QThread.finished comes after done/failed (same thread, queued in
        # order), i.e. after the dataset is saved and shown.
        worker.finished.connect(lambda w=worker: self._queue_after(w))

    def _queue_after(self, worker):
        if self._queue is None:
            return
        entry = self._queue[self._queue_i]
        outcome = worker.outcome or "error"
        self.queue_results.append((entry.name, outcome))
        if outcome == "error":
            # A module that died fails the next scan the same way: stop here.
            self._queue_stop = self._queue_stop or f"'{entry.name}' failed: {worker.error}"
            self._qlog(f"queue: scan {self._queue_i + 1} '{entry.name}' FAILED "
                       f"({worker.error}); queue stopped")
        else:
            self._qlog(f"queue: scan {self._queue_i + 1} '{entry.name}' {outcome}")
        # Next scan from the event loop, not from inside this signal: the old
        # worker's slots (done/failed) have all run by then.
        QtCore.QTimer.singleShot(0, self._queue_next)

    def _queue_end(self):
        q = self._queue or []
        counts = {}
        for _, outcome in self.queue_results:
            counts[outcome] = counts.get(outcome, 0) + 1
        parts = [f"{counts[k]} {k}" for k in ("done", "aborted", "error") if counts.get(k)]
        not_run = len(q) - len(self.queue_results)
        if not_run:
            parts.append(f"{not_run} not run")
        text = "Queue finished: " + ", ".join(parts or ["nothing ran"])
        if self._queue_stop:
            text += f"  --  {self._queue_stop}"
        self._qlog(text.replace("--", "-"))
        self.queue_lbl.setText(text)
        self._queue = None
        self.stop_queue_btn.hide()
        self._run_finished()
        self._rebuild_summary()                 # back to the editor's definition
        self.queue_lbl.setText(text)

    def autosave_path(self, recipe) -> Path | None:
        """One file per run: <data dir>/<date>/<time>_<name>.nc.

        Dated folders because a day's scans belong together, and the time in
        the name because a scan is normally repeated with one thing changed --
        overwriting the previous one is how an afternoon's work disappears.
        """
        if not self.autosave_dir:
            return None
        now = datetime.now()
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", recipe.name or "scan")
        path = (Path(self.autosave_dir) / now.strftime("%Y-%m-%d")
                / f"{now.strftime('%H%M%S')}_{safe}.nc")
        # Two scans of a queue can start within one second (a short scan, or
        # one aborted at once): a counter, never an overwrite.
        k = 2
        while path.exists():
            path = path.with_name(f"{now.strftime('%H%M%S')}_{safe}_{k}.nc"); k += 1
        return path

    def check_save_target(self) -> tuple[bool, str]:
        """Can the next scan actually be written? Returns (ok, message).

        It TRIES: makes the dated folder and writes a probe file next to where
        the data will go. Nothing else is proof -- a folder can exist and be
        read-only, a network share can be disconnected, a drive can be full-ish,
        and every one of those only shows up at save time, which on a long scan
        is an hour after you walked away.

        A missing file is the point of this: the file itself cannot be created
        early, because its name carries the time the scan STARTS.
        """
        if not self.autosave_dir:
            return False, "no data directory set — this run will not be saved"
        folder = Path(self.autosave_dir) / datetime.now().strftime("%Y-%m-%d")
        # Probe the dated folder if it is already there, otherwise the nearest
        # parent that exists -- being allowed to write in the parent is what
        # "we can create the dated folder" means. Deliberately NOT mkdir here:
        # this runs whenever the name is edited, and it must not leave an empty
        # dated folder behind on a day when nothing was measured.
        target = folder
        while not target.exists() and target.parent != target:
            target = target.parent
        probe = target / f".write_test_{os.getpid()}"
        try:
            probe.write_bytes(b"aaltoflow")
            probe.unlink()
        except OSError as exc:
            return False, f"CANNOT SAVE in {target}: {exc.strerror or exc}"
        return True, f"will save to {self.preview_path()}"

    def preview_path(self) -> str:
        """The file the next run would produce, with <time> left as a placeholder
        (the real one is stamped when Run is pressed)."""
        if not self.autosave_dir:
            return ""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_",
                      self.name_edit.text().strip() or "scan")
        return str(Path(self.autosave_dir) / datetime.now().strftime("%Y-%m-%d")
                   / f"<time>_{safe}.nc")

    def _refresh_save_target(self) -> bool:
        """Show where the next run goes, and whether it can be written there."""
        ok, msg = self.check_save_target()
        self.save_lbl.setText(msg)
        self.save_lbl.setStyleSheet(
            f"color:{C['muted'] if ok else C['danger']}; font-size:11px;")
        return ok

    #: Set to False by a test (or a headless caller) that must never block on a
    #: dialog; the run then goes ahead unsaved, as the button says.
    ask_before_unsaved = True

    def _confirm_unsaved(self, why: str) -> bool:
        """Say what is wrong and let the operator decide. Returns True to run."""
        self._refresh_save_target()
        if not self.ask_before_unsaved:
            return True
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("This scan cannot be saved")
        box.setText(why)
        box.setInformativeText(
            "Fix the data directory on the Settings tab, or run anyway — the "
            "result stays in memory and can be written with 'Save data (.nc)…', "
            "but nothing is written while it runs and nothing survives a crash.")
        run_anyway = box.addButton("Run without saving", QtWidgets.QMessageBox.DestructiveRole)
        box.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is run_anyway

    def _on_saved(self, path: str, done: int, total: int):
        self.last_saved = Path(path)
        self.save_lbl.setText(f"saved {done}/{total} points to {path}" if done < total
                              else f"saved to {path}")

    def _on_progress(self, done, total, eta):
        self.progress.setFormat("%p%")      # the points have started
        self.progress.setMaximum(total); self.progress.setValue(done)
        if self.queue_running():
            self._queue_label(eta)

    def _on_log(self, msg: str):
        """A routine step. Shown IN the progress bar -- which otherwise sits at
        0 % through a two-minute magnet ramp and reference sweep, saying nothing
        about why -- and passed to the suite's log."""
        self.run_log.append(msg)
        self.progress.setFormat(msg)
        if self.on_log is not None:
            self.on_log(msg)

    def _abort(self):
        if self.worker:
            self.worker.abort()

    def is_aborting(self) -> bool:
        """True once Abort was pressed, until the run ends.

        The suite hands this to `Lab.set_abort` so an instrument stuck in a
        settle wait notices the Abort too, instead of the window looking frozen
        until the wait times out.
        """
        return bool(self.worker is not None and self.worker._abort)

    def _on_partial(self, ds):
        """A snapshot mid-run: same dataset, points not measured yet are NaN."""
        self.dataset = ds
        self._fill_det_combo(ds)

    def _fill_det_combo(self, ds):
        """Hand the dataset to the viewer (it keeps the operator's choices)."""
        self.view.set_dataset(ds)

    def _on_done(self, ds):
        self.dataset = ds
        self._run_finished()
        self.progress.setValue(self.progress.maximum() or 1)
        self._fill_det_combo(ds)

    def _on_failed(self, message: str):
        """A refusal, a timeout, or Abort. Whatever it was, the RUN IS OVER --
        so say why and hand the buttons back. Leaving Run disabled here is how
        'I aborted, changed the range, and it would not start again' happens."""
        self.detail.setText(message)
        self._run_finished()

    def _run_finished(self):
        self.progress.setFormat("%p%")
        # Between two scans of a queue Run stays off: the queue is still going.
        self.run_btn.setEnabled(not self.queue_running())
        self.abort_btn.setEnabled(False)
        self.worker = None          # is_aborting() is False again for the next run

    def _update_plot(self):
        """Redraw with whatever the viewer's controls currently say."""
        self.view.refresh()

    # ---- dialogs ----------------------------------------------------------
    def _load_dialog(self):
        fns, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Load scan definition(s)", "",
            "Scan definition or queue (*.yaml *.yml *.nc);;Recipe or queue (*.yaml *.yml);;"
            "Measurement (*.nc);;All files (*)")
        if not fns:
            return
        if len(fns) == 1 and not scan_queue.is_queue_file(fns[0]):
            self._load_single(fns[0])
        else:
            self.open_queue(fns)

    def open_queue(self, paths) -> "QueueDialog | None":
        """Load several definitions (or a queue file) into the queue dialog;
        run them if the operator says so. Returns the dialog (tests)."""
        try:
            entries = scan_queue.load_definitions(paths)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Load scan queue", str(exc))
            return None
        self.refresh_axis_limits()          # validate against the live limits
        dlg = QueueDialog(entries, self.registry, self.per_pt.value(), self)
        if dlg.exec() and dlg.run_requested:
            self.run_queue(dlg.entries())
        return dlg

    def _load_single(self, fn):
        try:
            recipe = self.recipe_from_file(fn)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Load scan definition",
                                          f"Could not read {Path(fn).name}:\n{exc}")
            return
        missing = self.load_recipe(recipe)
        name = Path(fn).name
        if missing:
            # Naming them beats a half-filled stack with no explanation: the
            # usual cause is a module that is simply not connected right now.
            connected = {split_id(p.id)[0] for p in self.registry.settables()}
            connected |= {split_id(p.id)[0] for p in self.registry.gettables()}
            connected |= {split_id(a.id)[0] for a in self.registry.actions()}
            lines = []
            for m in missing:
                module = split_id(m)[0]
                why = ("this module is not connected" if module and module not in connected
                       else "not offered by the module as it is set up now")
                lines.append(f"  • {m} — {why}")
            QtWidgets.QMessageBox.warning(
                self, "Loaded with missing parameters",
                f"{name} was loaded, but these are NOT available here:\n\n"
                + "\n".join(lines)
                + "\n\nStart the module (Settings tab: Connect all running) and "
                  "load again to get them back.")
            self.detail.setText(f"{name}: not available -- " + ", ".join(missing))
        else:
            self.detail.setText(f"loaded {name}")

    def _save_dialog(self):
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save recipe", "scan.yaml", "YAML (*.yaml)")
        if fn:
            self.build_recipe().save(fn)

    def _save_data_dialog(self):
        if self.dataset is None:
            return
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save data", "scan.nc", "netCDF (*.nc)")
        if fn:
            self.dataset.to_netcdf(fn)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="AaltoFlow Scan Builder")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="UI theme for this launch (default: %s)" % DEFAULT_THEME)
    args = ap.parse_args()

    set_theme(args.theme or DEFAULT_THEME)      # BEFORE any widget / pg config is read
    pg.setConfigOption("background", C["code_bg"])
    pg.setConfigOption("foreground", C["text"])

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from apps.theme import apply_window_icon
    apply_window_icon(app)
    apply(app)
    win = ScanBuilder()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
