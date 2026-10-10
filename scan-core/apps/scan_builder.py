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
from scan_core.errors import RoutineError, ScanAborted, ScanFault
from scan_core.preview import preview_axis, step_summary
from scan_core.flyscan import find_speed_param, fly_axis, row_seconds
from scan_core import autosave, scan_queue
from suite_common import title as suite_title
from suite_common.fileio import replace_retry
from apps.data_view import DataView
from apps.image_live import LiveImage
from apps.run_info_card import RunInfoCard
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


def _axis_tag(text: str, tip: str = "") -> QtWidgets.QLabel:
    """A small amber TAG on an axis row: one setting that is not the default.

    Lukas's rule for the Advanced panel (2026-10-09): nothing may be hidden
    silently. A row whose fly speed or scout step is tucked away in a closed
    panel still SAYS so, in its own line, where the eye already is."""
    t = QtWidgets.QLabel(text)
    t.setObjectName("axisTag")
    t.setStyleSheet(
        f"QLabel#axisTag {{ color:{C['accent']}; border:1px solid {C['accent_dim']};"
        f" border-radius:8px; padding:1px 7px; font-size:10px; font-weight:600; }}")
    t.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
    if tip:
        t.setToolTip(tip)
    return t


def _gear_icon() -> QtGui.QIcon:
    """A small gear, drawn in the theme's text colour (read at call time, so
    it follows the theme set before the widgets are built)."""
    px = QtGui.QPixmap(32, 32)
    px.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(px)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    col = QtGui.QColor(C["text"])
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(col)
    p.translate(16, 16)
    for k in range(8):                    # eight teeth
        p.save()
        p.rotate(45 * k)
        p.drawRoundedRect(QtCore.QRectF(-3.2, -15, 6.4, 8), 1.2, 1.2)
        p.restore()
    p.drawEllipse(QtCore.QPointF(0, 0), 10.5, 10.5)
    p.setCompositionMode(QtGui.QPainter.CompositionMode_Clear)
    p.drawEllipse(QtCore.QPointF(0, 0), 4.5, 4.5)   # the hole
    p.end()
    return QtGui.QIcon(px)


def _muted(text: str, small: bool = False) -> QtWidgets.QLabel:
    w = QtWidgets.QLabel(text)
    w.setStyleSheet(f"color:{C['muted']};" + (" font-size:10px;" if small else ""))
    return w


class AdvancedPanel(QtWidgets.QFrame):
    """The ADVANCED panel of one axis row: opened IN PLACE under its row.

    An expander, not a dialog (Lukas approved the mockup, 2026-10-09): the
    settings stay next to the axis they belong to, and the rows below are
    pushed down rather than squeezed. The groups (FLY, SCOUT, POINT) sit side
    by side and fall into one column when the card is too narrow for that --
    the same rule as the routines card, decided from the groups' MEASURED
    widths, so a translated or longer label cannot cut a group off.
    """

    def __init__(self):
        super().__init__()
        self.setObjectName("axisAdvanced")
        self.setStyleSheet(
            f"QFrame#axisAdvanced {{ border-top: 1px dashed {C['border']}; }}")
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 6, 0, 4); outer.setSpacing(14)
        self.groups_box = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.LeftToRight)
        self.groups_box.setSpacing(16)
        outer.addLayout(self.groups_box, 1)
        self.side = QtWidgets.QVBoxLayout(); self.side.setSpacing(4)
        outer.addLayout(self.side, 0)
        self.groups: list = []

    def add_group(self, title: str):
        """A titled group; returns (frame, grid) -- fill the grid."""
        box = QtWidgets.QFrame()
        v = QtWidgets.QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(3)
        head = QtWidgets.QLabel(title)
        head.setStyleSheet(f"color:{C['accent']}; font-weight:800; font-size:10px;"
                           f" letter-spacing:1px;")
        v.addWidget(head)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(6); grid.setVerticalSpacing(3)
        v.addLayout(grid)
        v.addStretch(1)
        self.groups.append(box)
        # left-aligned: stacked in one column, a group keeps its own width
        # instead of being stretched across the card
        self.groups_box.addWidget(box, 0, QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        return box, grid

    def arrange(self) -> None:
        """Side by side if the groups fit, else one column."""
        shown = [g for g in self.groups if not g.isHidden()]
        if not shown:
            return
        need = (sum(g.sizeHint().width() for g in shown)
                + self.groups_box.spacing() * (len(shown) - 1))
        side = self.layout().itemAt(1).sizeHint().width() + self.layout().spacing()
        wide = need <= self.width() - side
        want = (QtWidgets.QBoxLayout.LeftToRight if wide
                else QtWidgets.QBoxLayout.TopToBottom)
        if self.groups_box.direction() != want:
            self.groups_box.setDirection(want)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.arrange()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.arrange()


class _StackRow(QtWidgets.QFrame):
    """What every row of the axis stack shares: the loop-level number, the
    indentation, the amber TAGS and the "Advanced" expander.

    Layout: the frame's own layout is a column -- the row line on top, the
    Advanced panel (hidden until opened) under it. The INDENT is the column's
    left margin, so the panel is indented with its row and reads as part of it.
    """
    changed = QtCore.Signal()
    remove = QtCore.Signal(object)
    move = QtCore.Signal(object, int)      # (self, +1/-1)
    preview = QtCore.Signal(object)        # double-click: show the actual setpoints
    #: the Advanced panel opened (True) or closed (False); the builder keeps
    #: only one open at a time
    advanced_toggled = QtCore.Signal(object, bool)
    #: a line for the suite's log (what "Copy from axis" did and skipped)
    log = QtCore.Signal(str)

    #: Pixels of indent per nesting level, and how many levels get one. Loop
    #: depth is the thing an operator misreads most often -- "which of these is
    #: the slow one?" -- and a number in a column is easy to skim past, while a
    #: staircase is not. Capped: at five axes an uncapped indent would push the
    #: spin boxes off the card.
    INDENT_PX = 16
    INDENT_MAX = 4
    #: closed: a gear icon + "Advanced" (the gear is DRAWN, _gear_icon: the
    #: Windows UI font has no glyph for U+2699 and showed an empty box)
    ADV_CLOSED = "Advanced"
    ADV_OPEN = "^ Advanced"

    def _start_layout(self) -> QtWidgets.QHBoxLayout:
        self.setObjectName("axis")
        self._root = QtWidgets.QVBoxLayout(self)
        self._root.setContentsMargins(10, 0, 10, 0); self._root.setSpacing(0)
        lay = QtWidgets.QHBoxLayout()
        lay.setContentsMargins(0, 6, 0, 6); lay.setSpacing(8)
        self._root.addLayout(lay)
        self.line = lay
        self.level_lbl = QtWidgets.QLabel("0")
        self.level_lbl.setStyleSheet(f"color:{C['accent']}; font-weight:800;")
        self.level_lbl.setFixedWidth(16)
        lay.addWidget(self.level_lbl)
        return lay

    def _finish_line(self, lay) -> None:
        """Tags (taking the free room), Advanced, and the row's own buttons."""
        # The tags take the stretch: they CLIP when there is no room and never
        # widen the row (the from/to/pts boxes keep their width), and nothing
        # to their right moves when one appears.
        self.tags = QtWidgets.QWidget()
        self.tags.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                QtWidgets.QSizePolicy.Preferred)
        self.tags_box = QtWidgets.QHBoxLayout(self.tags)
        self.tags_box.setContentsMargins(4, 0, 0, 0); self.tags_box.setSpacing(4)
        self.tags_box.addStretch(1)
        lay.addWidget(self.tags, 1)
        self.adv_btn = QtWidgets.QPushButton(self.ADV_CLOSED)
        self._gear = _gear_icon()
        self.adv_btn.setIcon(self._gear)
        self.adv_btn.setCheckable(True)
        self.adv_btn.setFixedWidth(104)
        self.adv_btn.setToolTip("Advanced settings of this axis: fly, scout, its name\n"
                                "in the file. Opens under the row.")
        self.adv_btn.toggled.connect(self.set_advanced_open)
        lay.addWidget(self.adv_btn)
        up = QtWidgets.QPushButton("↑"); dn = QtWidgets.QPushButton("↓")
        rm = QtWidgets.QPushButton("✕"); rm.setObjectName("danger")
        for b in (up, dn, rm):
            b.setFixedWidth(30)
        up.clicked.connect(lambda: self.move.emit(self, -1))
        dn.clicked.connect(lambda: self.move.emit(self, +1))
        rm.clicked.connect(lambda: self.remove.emit(self))
        lay.addWidget(up); lay.addWidget(dn); lay.addWidget(rm)
        self.advanced = AdvancedPanel()
        self.advanced.setVisible(False)
        self._root.addWidget(self.advanced)
        self.adv_note = _muted("", small=True)
        self.adv_note.setWordWrap(True)
        self.adv_note.setFixedWidth(112)

    def _point_group(self, with_name: bool = True):
        """POINT: the per-axis extras the recipe really has -- the name of the
        axis in the data file, and the routines bound to this axis (shown;
        they are edited in ROUTINES > THROUGHOUT). Deliberately short: there
        is no per-axis dwell or settle timeout in the engine, so none is
        offered here."""
        self.point_group, g = self.advanced.add_group("POINT")
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setFixedWidth(120)
        self.name_edit.setToolTip(
            "The name of this axis in the data file (its dimension and\n"
            "coordinate). Empty = the parameter's id. Two axes may not share\n"
            "a name.")
        self.name_edit.editingFinished.connect(self.changed.emit)
        self.name_lbl = _muted("name in the file")
        if with_name:
            g.addWidget(self.name_lbl, 0, 0); g.addWidget(self.name_edit, 0, 1)
        else:
            self.name_lbl.hide(); self.name_edit.hide()
        g.addWidget(_muted("routines"), 1, 0, QtCore.Qt.AlignTop)
        self.routines_lbl = QtWidgets.QLabel("")
        self.routines_lbl.setWordWrap(True)
        self.routines_lbl.setFixedWidth(170)
        self.routines_lbl.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
        g.addWidget(self.routines_lbl, 1, 1)
        self.set_axis_routines([])

    # ---- the expander -------------------------------------------------------
    def set_advanced_open(self, on: bool) -> None:
        on = bool(on)
        self.adv_btn.blockSignals(True)
        self.adv_btn.setChecked(on)
        self.adv_btn.blockSignals(False)
        self.adv_btn.setText(self.ADV_OPEN if on else self.ADV_CLOSED)
        self.adv_btn.setIcon(QtGui.QIcon() if on else self._gear)
        was = self.advanced.isVisibleTo(self)
        self.advanced.setVisible(on)
        if on:
            self.advanced.arrange()
        if was != on:
            self.advanced_toggled.emit(self, on)

    def advanced_open(self) -> bool:
        return self.advanced.isVisibleTo(self)

    def set_axis_routines(self, texts: list[str]) -> None:
        self.routines_lbl.setText(
            "\n".join(texts) if texts else
            "none on this axis (ROUTINES > THROUGHOUT: start or end of each sweep)")

    # ---- tags -----------------------------------------------------------------
    def tag_texts(self) -> list[str]:
        return []

    def refresh_tags(self) -> None:
        texts = self.tag_texts()
        have = [self.tags_box.itemAt(i).widget() for i in range(self.tags_box.count() - 1)]
        if [w.text() for w in have] == texts:
            return
        for w in have:
            self.tags_box.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        for i, t in enumerate(texts):
            self.tags_box.insertWidget(i, _axis_tag(t, "set in Advanced"), 0,
                                       QtCore.Qt.AlignVCenter)

    def dim_name(self) -> str:
        """The name typed in POINT ("" = the default)."""
        return self.name_edit.text().strip()

    def _indent(self, level: int) -> None:
        _, top, right, bottom = self._root.getContentsMargins()
        self._root.setContentsMargins(10 + self.INDENT_PX * min(level, self.INDENT_MAX),
                                      top, right, bottom)


class AxisRow(_StackRow):
    """One axis of the stack: parameter, from / to / pts -- and, behind
    "Advanced", how it is swept (fly, scout, its name in the file)."""

    #: the keys of a linear / fly axis the row models; anything else a loaded
    #: axis carries is kept in `extra` and written back unchanged
    MODELLED = {"type", "param", "start", "stop", "num", "step", "speed",
                "row_time_s", "speed_param", "move", "readback", "lag_correction", "timeout_s",
                "collapse", "collapse_keep_pixels", "name"}

    def __init__(self, param, level_getter, speed_param=None, move_choices=(),
                 speed_lookup=None, registry=None):
        super().__init__()
        self.param = param
        self._raw = None                   # set for non-editable (raster/zip) rows
        self._level_getter = level_getter
        self._registry = registry
        self._zigzag = False
        #: unmodelled keys of a loaded axis (written back as they came)
        self.extra: dict = {}
        #: a loaded scout margin that differs between the dims of one row (a
        #: raster's x and y): kept as loaded until the margin is edited
        self._loaded_margins: dict | None = None
        lay = self._start_layout()

        # Name on top, the live limit envelope as a caption beneath it.
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
        self.num.setFixedWidth(84)
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
            if w is self.num:
                self.num_lbl = tl

        # A spin box that silently refuses to go above 160 is baffling unless
        # you can see that 160 is the closed-loop ceiling -- and these limits
        # MOVE (piezo CL/OL, kim's leash, clMag's calibration), so showing the
        # number beats making the operator guess.
        self._sync_limits_label()
        self._finish_line(lay)

        self._build_fly(speed_param, move_choices, speed_lookup)
        self._build_scout()
        self._point_group(with_name=True)
        self.name_edit.setPlaceholderText(param.id)
        self._build_side()
        self.changed.connect(self.refresh_tags)
        self._fly_toggled(False)
        self._scout_toggled(False)

    # ---- raw (raster / zip / array) rows ------------------------------------
    @property
    def raw(self):
        return self._raw

    @raw.setter
    def raw(self, value):
        """A loaded raster / zip / array axis is passed through as it was:
        it cannot fly and has no top-level name to edit here (a raster names
        its x and y inside), so those parts of Advanced are hidden."""
        self._raw = value
        plain = value is None
        self.fly_group.setVisible(plain)
        self.name_lbl.setVisible(plain)
        self.name_edit.setVisible(plain)
        self.refresh_tags()

    # ---- FLY ---------------------------------------------------------------------
    def _build_fly(self, speed_param, move_choices=(), speed_lookup=None):
        """FLY: move continuously across this axis instead of stopping at every
        point (scan_core/flyscan.py). Every fly-related setting of the recipe
        is here: on/off, speed, the knob that sets the speed, the stage that
        flies a MEASURED coordinate, the direction of the rows, the row timeout,
        the lag correction and the position the samples are binned by.

        Offered only for a position whose module STREAMS it -- binning by the
        measured position is the whole idea, so without a recorded position
        there is nothing to bin by -- and only meaningful on the innermost
        axis (the summary says so if it is ticked anywhere else). With it
        ticked, `pts` become pixels.

        A MEASURED COORDINATE (camera.laser_x: where the laser is on the sample)
        streams but is not a stage: `move_choices` lists the stages that could
        fly it, and the "move with" box picks one. The speed then belongs to
        the chosen stage (`speed_lookup(stage_id)` finds its speed knob).

        Every box is always there, greyed while fly is off: a panel that grew
        a line on ticking would move the controls under the mouse.
        """
        self.move_choices = list(move_choices or [])
        self._speed_lookup = speed_lookup or (lambda _id: None)
        self._knob_objs: dict = {}
        #: the module records this value continuously (a stage position, a
        #: camera coordinate): a fly row can be binned by it
        self.streamed = getattr(self.param, "stream", None) is not None
        #: the module can SWEEP this knob itself at a set pace (a `ramp` block
        #: in describe: clMag's field, dssg's frequency -- scan_core/ramp.py).
        #: Such a knob can be flown even when nothing streams it: the ramp
        #: brings its own record (the Hall probe, or the values it sent).
        self.ramp = getattr(self.param, "ramp", None)
        #: either way this axis can fly
        self.streams = self.streamed or self.ramp is not None
        self.fly_group, g = self.advanced.add_group("FLY")

        self.fly = QtWidgets.QCheckBox("fly this axis")
        self.fly.setEnabled(self.streams)
        self.fly.setToolTip(
            (f"FLY: the module sweeps {self.param.label} continuously from\n"
             "'from' to 'to' at the pace given, the detectors recording all\n"
             "the way; the samples are then averaged per pixel. Innermost axis\n"
             "only; every detector must be one its module can stream.")
            if self.ramp is not None else
            "FLY: move continuously from 'from' to 'to' at the speed given,\n"
            "recording the detectors and the MEASURED position all the way,\n"
            "then average the samples per pixel. Innermost axis only; every\n"
            "detector must be one its module can stream."
            if self.streams else
            f"{self.param.label} cannot be flown: this knob can neither stream\n"
            f"nor sweep (no stream and no ramp block in its module's describe):\n"
            f"step it.")
        self.speed = QtWidgets.QDoubleSpinBox()
        self.speed.setDecimals(3)
        self.speed.setFixedWidth(84)
        self.speed_lbl = _muted("")
        # ROW TIME: the other way to give the pace. A physicist often knows
        # "one field sweep should take a minute" better than "x mT/s"; the
        # engine turns row_time_s into the pace (flyscan.fly_rate). Exactly
        # one of speed / row time is written -- or neither, for a knob whose
        # module has a default sweep rate.
        self.row_time = QtWidgets.QDoubleSpinBox()
        self.row_time.setRange(0.1, 1e6); self.row_time.setDecimals(1)
        self.row_time.setSuffix(" s"); self.row_time.setValue(60.0)
        self.row_time.setFixedWidth(84)
        self.row_time.setToolTip("How long ONE row (from 'from' to 'to', half a pixel\n"
                                 "past each end) should take; the pace follows.")
        self.pace_box = QtWidgets.QComboBox()
        self.pace_box.setToolTip(
            "speed: the pace in the knob's unit per second.\n"
            "row time: how long one row takes; the pace follows from the span.\n"
            "module default: the sweep rate the module declares (written as\n"
            "neither, so the module's own default is used).")
        sp_box = QtWidgets.QHBoxLayout(); sp_box.setSpacing(4)
        sp_box.addWidget(self.speed); sp_box.addWidget(self.row_time)
        sp_box.addWidget(self.speed_lbl)
        self.row_time.hide()

        self.move_box = QtWidgets.QComboBox()
        if self.ramp is not None and self.move_choices:
            # a knob the module sweeps itself: its own ramp comes first; a
            # stage flying it in these coordinates is still offered
            self.move_box.addItem("(its module sweeps it)", None)
        for mid in self.move_choices:
            self.move_box.addItem(mid, mid)
        self.move_box.setToolTip(
            "The stage that flies this row. The grid, the placement of each row\n"
            "and the binning stay in THIS parameter's coordinates; which way the\n"
            "stage has to go is learned on the first row. If the scan stops with\n"
            "'does not move', pick the other axis (the camera may be mounted\n"
            "rotated against the stage).")
        self.move_lbl = _muted("move with")

        self.knob_box = QtWidgets.QComboBox()
        self.knob_box.setMinimumWidth(110)
        self.knob_box.setToolTip(
            "The setting that sets the stage's speed. It is set to the fly speed\n"
            "for each row and put back for the approach and at the end.\n"
            "(none): the stage moves at whatever speed it has; the number is\n"
            "then only used for the time estimate.")

        self.dir_box = QtWidgets.QComboBox()
        self.dir_box.addItem("one-way", False)
        self.dir_box.addItem("zig-zag", True)
        self.dir_box.setToolTip(
            "zig-zag: every other row is flown BACKWARDS -- the fly-back saved,\n"
            "and the best check of the lag correction (a forward and a backward\n"
            "row must put an edge in the same place).\n"
            "This is the scan's zig-zag setting (the box next to Run): it also\n"
            "reverses every other pass of stepped inner axes.")
        self.dir_box.currentIndexChanged.connect(
            lambda *_: self._dir_changed())

        self.timeout_auto = QtWidgets.QCheckBox("auto")
        self.timeout_auto.setChecked(True)
        self.timeout_spin = QtWidgets.QDoubleSpinBox()
        self.timeout_spin.setRange(1.0, 1e6); self.timeout_spin.setDecimals(0)
        self.timeout_spin.setSuffix(" s"); self.timeout_spin.setValue(120.0)
        self.timeout_spin.setFixedWidth(84)
        tip = ("How long one row may take before the scan stops with an error.\n"
               "auto: 3 x (row length / speed) + 30 s.")
        self.timeout_auto.setToolTip(tip); self.timeout_spin.setToolTip(tip)
        to_box = QtWidgets.QHBoxLayout(); to_box.setSpacing(4)
        to_box.addWidget(self.timeout_auto); to_box.addWidget(self.timeout_spin)

        self.lag_box = QtWidgets.QCheckBox("lag correction")
        self.lag_box.setChecked(True)
        self.lag_box.setToolTip(
            "A lock-in's output belongs to where the stage was a moment EARLIER\n"
            "(its filter delay). On: each sample is moved back by the delay its\n"
            "module declares before its position is looked up. Off only to see\n"
            "the raw, shifted rows.")

        self.readback_box = QtWidgets.QComboBox()
        self.readback_box.addItem("(this axis)", None)
        if self._registry is not None:
            for q in [*self._registry.settables(), *self._registry.gettables()]:
                if (q.id != self.param.id and getattr(q, "stream", None) is not None
                        and q.unit == self.param.unit
                        and self.readback_box.findData(q.id) < 0):
                    self.readback_box.addItem(q.id, q.id)
        self.readback_box.setToolTip(
            "The measured position the samples are binned by. Normally the axis\n"
            "itself; another streamed position in the same unit if that is the\n"
            "better measurement (a sensor rather than a step counter).")

        # What the samples of a row are sorted into pixels by. "measurement":
        # a value the instrument MEASURED while moving (a stage's position, a
        # Hall probe); "command": only what was SENT, with its time stamp (a
        # generator cannot report its frequency mid-sweep). A reader of the
        # data must be able to tell the two apart, so it is shown here, on
        # the row's tag and in the file (fly_binned_by).
        self.binned_lbl = _muted("", small=True)
        self.binned_lbl.setToolTip(
            "binned by measurement: each sample is placed by the value the\n"
            "instrument MEASURED at that moment.\n"
            "binned by command: by the value the module SENT at that moment --\n"
            "right when the instrument follows its commands quickly.")
        # ONE MEAN PER ROW (flyscan.collapse_rows): the row as a way of
        # collecting samples -- fly the field across a window and keep one
        # number per outer point. The pixels stay in the file unless "keep
        # pixel data" is unticked (only meaningful with the row mean on).
        self.collapse_box = QtWidgets.QCheckBox("collapse to one mean per row")
        self.collapse_box.setToolTip(
            "Also store, for every row, the mean over ALL the pixels of the row\n"
            "(<detector>_rowmean, with _rowmean_n samples and _rowmean_std).\n"
            "Weighted by the samples: the mean of every sample the row recorded,\n"
            "not the mean of the pixel means. Its spread includes the variation\n"
            "ALONG the row. A trace keeps its own axis (one mean trace per row).\n"
            "With a repeat set to average, the repeats are pooled first.")
        self.keep_box = QtWidgets.QCheckBox("keep pixel data")
        self.keep_box.setChecked(True)
        self.keep_box.setToolTip(
            "On (default): the file holds the pixels AND the row means.\n"
            "Off: only the row means are saved -- for a long scan where only\n"
            "the mean matters. The live plot shows the pixels either way.")
        self.fly_hint = _muted("", small=True)
        self.fly_hint.setWordWrap(True)
        self.knob_lbl = _muted("speed knob")
        self.readback_lbl = _muted("readback")
        g.addWidget(self.fly, 0, 0, 1, 2)
        g.addWidget(self.pace_box, 0, 2); g.addLayout(sp_box, 0, 3)
        g.addWidget(self.knob_lbl, 1, 0); g.addWidget(self.knob_box, 1, 1)
        g.addWidget(self.move_lbl, 1, 2); g.addWidget(self.move_box, 1, 3)
        g.addWidget(_muted("direction"), 2, 0); g.addWidget(self.dir_box, 2, 1)
        g.addWidget(_muted("row timeout"), 2, 2); g.addLayout(to_box, 2, 3)
        g.addWidget(self.lag_box, 3, 0, 1, 2)
        g.addWidget(self.readback_lbl, 3, 2); g.addWidget(self.readback_box, 3, 3)
        # the long label spans three columns, so the FLY group gets no wider
        # (a wider group would stack the Advanced groups one under the other
        # on the suite's Scan tab)
        g.addWidget(self.collapse_box, 4, 0, 1, 3)
        g.addWidget(self.keep_box, 4, 3)
        g.addWidget(self.binned_lbl, 5, 0, 1, 4)
        g.addWidget(self.fly_hint, 6, 0, 1, 4)
        if not self.move_choices:
            # a stage flies itself: nothing to choose
            self.move_lbl.setText("")
            self.move_box.hide()
        if not self.streams:
            self.fly_hint.setText("Cannot fly: this knob can neither stream nor "
                                  "sweep -- step it.")
        else:
            self.fly_hint.hide()

        #: the knob find_speed_param proposed (the default to compare against)
        self._default_knob = speed_param.id if speed_param is not None else None
        if speed_param is not None:
            self._knob_objs[speed_param.id] = speed_param
        #: which way this row flies now: "ramp" (its module sweeps it) or
        #: "stage" (a position moving at a speed of its own) -- None until set
        self._path = None
        self._fill_pace("speed")
        self._fill_knobs(self._default_knob)
        current = float("nan")
        if speed_param is not None:
            try:
                current = float(speed_param.get())
            except Exception:
                pass
        self._stage_speed0 = current
        self._sync_path()                        # sets the speed box's default
        self._default_speed = self.speed.value()
        if self.move_choices:
            self.move_box.currentIndexChanged.connect(lambda *_: self._move_changed())
            self._move_changed()
        self.knob_box.currentIndexChanged.connect(lambda *_: self._knob_changed())
        self.fly.toggled.connect(self._fly_toggled)
        self.speed.valueChanged.connect(lambda *_: self.changed.emit())
        self.row_time.valueChanged.connect(lambda *_: self.changed.emit())
        self.pace_box.currentIndexChanged.connect(lambda *_: self._pace_changed())
        self.timeout_auto.toggled.connect(lambda *_: self._fly_toggled(self.is_fly()))
        self.timeout_spin.valueChanged.connect(lambda *_: self.changed.emit())
        self.lag_box.toggled.connect(lambda *_: self.changed.emit())
        # the keep box greys out with the row mean off: re-run the enabling
        self.collapse_box.toggled.connect(lambda *_: self._fly_toggled(self.is_fly()))
        self.keep_box.toggled.connect(lambda *_: self.changed.emit())
        self.readback_box.currentIndexChanged.connect(lambda *_: self._readback_changed())

    def _readback_changed(self) -> None:
        # another measured value to bin by makes a ramp "binned by measurement"
        self.binned_lbl.setText(f"binned by {self.binned_by()}")
        self.changed.emit()

    def _moving_id(self) -> str:
        return self.move_param() or self.param.id

    def is_ramp(self) -> bool:
        """True when this row flies by its MODULE'S SWEEP (a `ramp` block),
        not as a stage. The engine decides the same way (flyscan.ramp_of): a
        knob with a ramp block flown with a 'move with' stage takes the stage
        path, because the recipe asked for that mechanism by name."""
        return self.ramp is not None and not self.move_param()

    def binned_by(self) -> str:
        """What the samples will be sorted into pixels by: "measurement" or
        "command" -- the engine's rule (flyscan.fly_sweep): a stage flies by
        its measured position; a ramp by its own readback, unless a readback
        override names another measured value."""
        if not self.is_ramp() or self.readback_box.currentData():
            return "measurement"
        return self.ramp.binned_by

    def pace(self) -> str:
        """How the pace is given: "speed", "row_time" or "default"."""
        return self.pace_box.currentData() or "speed"

    def _fill_pace(self, select: str) -> None:
        """The pace choices this row offers. "module default" only for a knob
        whose module sweeps it AND declares a default rate: a stage has none
        (a fly row there must say how fast)."""
        items = [("speed", "speed"), ("row time", "row_time")]
        if self.is_ramp() and self.ramp.rate_default is not None:
            items.append(("module default", "default"))
        self.pace_box.blockSignals(True)
        self.pace_box.clear()
        for text, key in items:
            self.pace_box.addItem(text, key)
        i = self.pace_box.findData(select)
        self.pace_box.setCurrentIndex(i if i >= 0 else 0)
        self.pace_box.blockSignals(False)

    def set_pace(self, mode: str) -> None:
        """Choose how the pace is given ("speed" / "row_time" / "default")."""
        i = self.pace_box.findData(mode)
        if i >= 0:
            self.pace_box.setCurrentIndex(i)

    def _pace_changed(self) -> None:
        self._update_pace_widgets()
        self.changed.emit()

    def _update_pace_widgets(self) -> None:
        """Show the box of the chosen pace; the unit label says what it means."""
        mode = self.pace()
        on = self.is_fly() and self.streams
        self.speed.setVisible(mode == "speed")
        self.row_time.setVisible(mode == "row_time")
        self.speed.setEnabled(on and mode == "speed")
        self.row_time.setEnabled(on and mode == "row_time")
        self.pace_box.setEnabled(on)
        if mode == "default":
            self.speed_lbl.setText(f"{self.ramp.rate_default:g} {self.speed_unit()}")
        elif mode == "row_time":
            self.speed_lbl.setText("per row")
        else:
            self.speed_lbl.setText(self.speed_unit())

    def _path_default_speed(self) -> float:
        """The speed box's starting value: the module's default sweep rate
        for a ramp, the stage's current speed for a stage (else 1)."""
        lo, hi = self.speed.minimum(), self.speed.maximum()
        if self.is_ramp():
            v = self.ramp.rate_default
        else:
            v = self._stage_speed0
            if not math.isfinite(v) and self.speed_param is not None:
                try:
                    v = float(self.speed_param.get())
                except Exception:
                    v = None
        v = float("nan") if v is None else float(v)
        return v if math.isfinite(v) and v > 0 else min(max(1.0, lo), hi)

    def _sync_path(self) -> None:
        """Show the boxes of the way this row flies.

        A RAMP has no use for the stage's boxes: no "speed knob" (the module
        sweeps at the rate it is given; naming a knob would even make the
        engine take the stage path), and no readback override unless the knob
        is itself streamed (the ramp brings its own record). The speed box
        takes the ramp's rate unit, limits and default."""
        path = "ramp" if self.is_ramp() else "stage"
        changed = path != self._path
        self._path = path
        ramp = path == "ramp"
        self.knob_lbl.setVisible(not ramp)
        self.knob_box.setVisible(not ramp)
        rb_show = self.streamed or self.readback_box.currentData() is not None
        self.readback_lbl.setVisible(rb_show)
        self.readback_box.setVisible(rb_show)
        self._fill_pace(self.pace())
        self._knob_changed(emit=False)           # range + unit
        if changed:
            self.speed.blockSignals(True)
            self.speed.setValue(self._path_default_speed())
            self.speed.blockSignals(False)
        self._update_pace_widgets()
        self.binned_lbl.setText(f"binned by {self.binned_by()}")

    def speed_unit(self) -> str:
        """The fly speed's unit: the ramp's rate unit when the module sweeps
        the knob, else the MOVING stage's unit per second."""
        if self.is_ramp():
            return self.ramp.rate_unit or f"{self.param.unit or ''}/s"
        mid = self._moving_id()
        p = self._registry.get(mid) if self._registry is not None else None
        unit = (getattr(p, "unit", None) if p is not None else None)
        if unit is None:
            unit = self.param.unit if mid == self.param.id else ""
        return f"{unit or ''}/s"

    def _knob_candidates(self, moving: str) -> list:
        """Settables that could set the speed of `moving`: same module, unit
        '<unit>/s' (find_speed_param's rule, without its axis-letter guess)."""
        reg = self._registry
        if reg is None:
            return []
        mp = reg.get(moving)
        if mp is None:
            return []
        unit = f"{getattr(mp, 'unit', '') or ''}/s"
        module = moving.rsplit(".", 1)[0] if "." in moving else ""
        return [q for q in reg.settables()
                if q.id != moving and (getattr(q, "unit", "") or "") == unit
                and (q.id.rsplit(".", 1)[0] if "." in q.id else "") == module]

    def _fill_knobs(self, select: str | None) -> None:
        self.knob_box.blockSignals(True)
        self.knob_box.clear()
        for q in self._knob_candidates(self._moving_id()):
            self._knob_objs[q.id] = q
            self.knob_box.addItem(q.id, q.id)
        if select is not None and self.knob_box.findData(select) < 0:
            self.knob_box.addItem(select if select in self._knob_objs
                                  else f"(missing) {select}", select)
        self.knob_box.addItem("(none)", None)
        self.knob_box.setCurrentIndex(max(0, self.knob_box.findData(select)))
        self.knob_box.blockSignals(False)
        self._knob_changed(emit=False)

    def set_speed_param(self, sp_id: str | None) -> None:
        """Select the speed knob (a loaded recipe's `speed_param`)."""
        if sp_id is not None and self.knob_box.findData(sp_id) < 0:
            p = self._registry.get(sp_id) if self._registry is not None else None
            if p is not None:
                self._knob_objs[sp_id] = p
            self.knob_box.insertItem(self.knob_box.count() - 1,
                                     sp_id if p is not None else f"(missing) {sp_id}",
                                     sp_id)
        self.knob_box.setCurrentIndex(max(0, self.knob_box.findData(sp_id)))

    @property
    def speed_param(self):
        """The Parameter that sets the fly speed (None = none, or missing)."""
        return self._knob_objs.get(self.knob_box.currentData())

    def speed_param_id(self) -> str | None:
        return self.knob_box.currentData()

    def _knob_changed(self, emit: bool = True) -> None:
        """The speed box's range follows the chosen knob's limits -- or, for
        a knob its module sweeps, the ramp's rate limits."""
        if self.is_ramp():
            rlo, rhi = self.ramp.rate_limits
            lo = max(0.001, rlo) if math.isfinite(rlo) else 0.001
            hi = min(1e6, rhi) if math.isfinite(rhi) else 1e6
            self.speed.setRange(lo, max(lo, hi))
            self.speed.setToolTip(
                f"The pace of the module's sweep, {self.speed_unit()}; it can sweep\n"
                f"between {rlo:g} and {rhi:g} {self.speed_unit()}.")
            self._update_pace_widgets()
            if emit:
                self.changed.emit()
            return
        sp = self.speed_param
        lo, hi = 0.001, 1e4
        if sp is not None:
            slo, shi = sp.limits
            lo = max(lo, float(slo)) if math.isfinite(slo) else lo
            hi = min(hi, float(shi)) if math.isfinite(shi) else hi
        self.speed.setRange(lo, max(lo, hi))
        self.speed.setToolTip(
            (f"Set on {sp.id} for the fly move; the old speed is put\n"
             f"back for the approach to each row and at the end.")
            if sp is not None else
            "No speed setting: the stage moves at whatever speed it has.\n"
            "This number is then only used for the time estimate.")
        self._update_pace_widgets()
        if emit:
            self.changed.emit()

    def _move_changed(self):
        """The flying stage changed: its speed knob sets the fly speed now."""
        sp = self._speed_lookup(self.move_box.currentData())
        if sp is not None:
            self._knob_objs[sp.id] = sp
        self._fill_knobs(sp.id if sp is not None else None)
        # a knob its module sweeps can also be flown by a stage: picking one
        # (or going back to the module's own sweep) changes the boxes shown
        self._sync_path()
        self.changed.emit()

    def move_param(self) -> str | None:
        return self.move_box.currentData() if self.move_choices else None

    def _fly_toggled(self, on):
        on = bool(on)
        for w in (self.move_box, self.knob_box, self.dir_box,
                  self.timeout_auto, self.lag_box, self.readback_box,
                  self.collapse_box):
            w.setEnabled(on and self.streams)
        self.timeout_spin.setEnabled(on and self.streams and not self.timeout_auto.isChecked())
        self.keep_box.setEnabled(on and self.streams and self.collapse_box.isChecked())
        self._update_pace_widgets()               # speed / row time / default
        self.num_lbl.setText("pixels" if on else "pts")
        self.changed.emit()

    def is_fly(self) -> bool:
        return self._raw is None and self.fly.isChecked()

    def _dir_changed(self):
        self._zigzag = bool(self.dir_box.currentData())
        self.zigzag_changed.emit(self._zigzag)
        self.changed.emit()

    #: the direction box was changed here: the builder sets the scan's zig-zag
    zigzag_changed = QtCore.Signal(bool)

    def set_zigzag(self, on: bool) -> None:
        """The scan's zig-zag, shown in this row's direction box."""
        on = bool(on)
        if on == self._zigzag and self.dir_box.currentData() == on:
            return
        self._zigzag = on
        self.dir_box.blockSignals(True)
        self.dir_box.setCurrentIndex(1 if on else 0)
        self.dir_box.blockSignals(False)
        self.refresh_tags()

    # ---- SCOUT -------------------------------------------------------------------
    def _build_scout(self):
        """SCOUT (scan_core/scout.py): look at this axis quickly first -- every
        k-th point -- and measure in detail only where the scout saw
        something. Any axis that moves something can be scouted, one or
        several. The margin is PER AXIS here (auto = half this axis's step);
        what the scout looks at and how it decides are scan-wide, in the SCOUT
        PASS section under the stack.

        The boxes are always there, greyed while the tick is off."""
        self.scout_group, g = self.advanced.add_group("SCOUT")
        self.scout = QtWidgets.QCheckBox("scout this axis")
        self.scout.setToolTip(
            "SCOUT: take a quick look along this axis first (every k-th point,\n"
            "always including the last one), then measure in detail only where\n"
            "the scout saw something. Tick it on every axis to scout.")
        self.scout_step = QtWidgets.QSpinBox()
        self.scout_step.setRange(1, 1000); self.scout_step.setValue(3)
        self.scout_step.setSuffix(" pts")
        self.scout_step.setFixedWidth(72)
        self.scout_step.setToolTip(
            "The scout looks at every k-th point of this axis (3 = every 3rd),\n"
            "and always at the last one. Something narrower than about k grid\n"
            "steps can fall between the scout's points: use a smaller k then.")
        self.margin_auto = QtWidgets.QCheckBox("auto")
        self.margin_auto.setChecked(True)
        self.margin_auto.setToolTip(
            "Half this axis's scout step (1.5 points for every 3rd): about how\n"
            "well the scout can place an edge. On the rig it lost no rim\n"
            "(2026-10-08).")
        self.margin_spin = QtWidgets.QDoubleSpinBox()
        self.margin_spin.setRange(0, 1000); self.margin_spin.setDecimals(1)
        self.margin_spin.setValue(2.0); self.margin_spin.setSuffix(" pts")
        self.margin_spin.setFixedWidth(84)
        self.margin_spin.setToolTip(
            "Grow the mask by this many GRID POINTS along this axis, so the\n"
            "edges are measured too.")
        g.addWidget(self.scout, 0, 0, 1, 3)
        g.addWidget(_muted("coarse step"), 1, 0); g.addWidget(self.scout_step, 1, 1)
        mbox = QtWidgets.QHBoxLayout(); mbox.setSpacing(4)
        mbox.addWidget(self.margin_auto); mbox.addWidget(self.margin_spin)
        g.addWidget(_muted("margin"), 2, 0); g.addLayout(mbox, 2, 1, 1, 2)
        self.scout_note = _muted("", small=True)
        self.scout_note.setWordWrap(True)
        self.scout_note.setFixedWidth(170)
        self.scout_note.hide()
        g.addWidget(self.scout_note, 3, 0, 1, 3)
        # two fixed lines, not word-wrapped: a wrapped label's height is
        # guessed before its width is known, and the last line was cut off
        hint = _muted("what it looks at, how it decides:\n"
                      "the SCOUT PASS section below", small=True)
        g.addWidget(hint, 4, 0, 1, 3)
        self.scout.toggled.connect(self._scout_toggled)
        self.scout_step.valueChanged.connect(lambda *_: self.changed.emit())
        self.margin_auto.toggled.connect(lambda *_: self._margin_edited())
        self.margin_spin.valueChanged.connect(lambda *_: self._margin_edited())

    def _scout_toggled(self, on):
        on = bool(on)
        self.scout_step.setEnabled(on)
        self.margin_auto.setEnabled(on)
        self.margin_spin.setEnabled(on and not self.margin_auto.isChecked())
        self.changed.emit()

    def _margin_edited(self):
        # an edit sets the margin of every dim of this row: a loaded margin
        # that differed between them (a raster's x and y) is replaced
        self._loaded_margins = None
        self.scout_note.hide()
        self.margin_spin.setEnabled(self.is_scout() and not self.margin_auto.isChecked())
        self.changed.emit()

    def is_scout(self) -> bool:
        return self.scout.isChecked()

    def scout_dims(self) -> list[str]:
        """The scan dims this row makes, X before Y for a raster (a picture's
        columns are the FIRST scouted axis, scout.picture_xy)."""
        ax = self.to_axis()
        if ax.get("type") == "raster":
            return [ax["x"].get("name") or ax["x"]["param"],
                    ax["y"].get("name") or ax["y"]["param"]]
        try:
            from scan_core.recipe import _compile_axis
            return [d.name for d in _compile_axis(ax) if d.params]
        except Exception:
            return []

    def _ui_margin(self):
        return "auto" if self.margin_auto.isChecked() else float(self.margin_spin.value())

    def scout_margins(self) -> dict:
        """{dim: "auto" | grid points} for the dims of this row."""
        dims = self.scout_dims()
        if self._loaded_margins is not None:
            return {d: self._loaded_margins.get(d, "auto") for d in dims}
        return {d: self._ui_margin() for d in dims}

    def set_scout_margin(self, margin) -> None:
        """Show a recipe's `margin` (auto | n | {dim: n}) for this row."""
        dims = self.scout_dims()
        vals = {d: (margin.get(d, "auto") if isinstance(margin, dict) else margin)
                for d in dims}
        uniq = list(dict.fromkeys(str(v) for v in vals.values()))
        first = next(iter(vals.values()), "auto")
        for w in (self.margin_auto, self.margin_spin):
            w.blockSignals(True)
        try:
            self.margin_auto.setChecked(first == "auto")
            if first != "auto":
                try:
                    self.margin_spin.setValue(float(first))
                except (TypeError, ValueError):
                    pass
        finally:
            for w in (self.margin_auto, self.margin_spin):
                w.blockSignals(False)
        if len(uniq) > 1:
            self._loaded_margins = vals
            self.scout_note.setText(
                "margin from the file: " + ", ".join(
                    f"{d} {v if v == 'auto' else f'{float(v):g} pts'}"
                    for d, v in vals.items()) + " -- an edit sets all of them")
            self.scout_note.show()
        else:
            self._loaded_margins = None
            self.scout_note.hide()
        self._scout_toggled(self.is_scout())

    # ---- copy / reset ----------------------------------------------------------
    def _build_side(self):
        self.copy_btn = QtWidgets.QToolButton()
        self.copy_btn.setText("Copy from axis…")
        self.copy_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.copy_btn.setToolTip(
            "Take the fly and scout settings of another axis. Only what makes\n"
            "sense here is copied (no um/s speed onto a frequency axis); what\n"
            "is skipped is said below and in the log.")
        menu = QtWidgets.QMenu(self.copy_btn)
        menu.aboutToShow.connect(lambda m=menu: self._fill_copy_menu(m))
        self.copy_btn.setMenu(menu)
        self.reset_btn = QtWidgets.QPushButton("Reset")
        self.reset_btn.setToolTip("Back to plain stepping: fly and scout off, every\n"
                                  "advanced setting to its default, the name cleared.")
        self.reset_btn.clicked.connect(self.reset_advanced)
        for w in (self.copy_btn, self.reset_btn):
            w.setFixedWidth(112)
        self.advanced.side.addWidget(self.copy_btn)
        self.advanced.side.addWidget(self.reset_btn)
        self.advanced.side.addWidget(self.adv_note)
        self.advanced.side.addStretch(1)

    def _siblings(self) -> list:
        """The rows around this one, outer first."""
        getter = getattr(self._level_getter, "__self__", None)
        return list(getattr(getter, "rows", []) or [])

    def _fill_copy_menu(self, menu) -> None:
        menu.clear()
        others = [r for r in self._siblings() if r is not self
                  and isinstance(r, AxisRow)]
        if not others:
            a = menu.addAction("(no other axis)")
            a.setEnabled(False)
            return
        for r in others:
            a = menu.addAction(f"{r.level_lbl.text()}  {r.param.label}")
            a.triggered.connect(lambda _=False, src=r: self.copy_from(src))

    def copy_from(self, src) -> str:
        """Copy the fly and scout settings of row `src` -- only those that make
        sense for THIS axis. Returns the line that is shown and logged.

        Skipped, and said so: fly onto an axis that cannot fly; a fly speed
        whose unit does not fit (um/s onto a frequency axis); a 'move with'
        stage this axis does not offer; anything from or onto a repeat row
        (it moves nothing). The name is never copied (two axes may not share
        one), nor the speed knob (it belongs to the stage that moves)."""
        copied, skipped = [], []
        if not isinstance(src, AxisRow):
            msg = (f"{self.param.label}: nothing copied -- a repeat row has no "
                   f"fly or scout settings")
            self._say(msg)
            return msg
        # FLY
        if self._raw is None:
            if src.is_fly():
                if not self.streams:
                    skipped.append("fly (this axis can neither stream nor sweep)")
                else:
                    self.fly.setChecked(True)
                    copied.append("fly")
                    # The PACE. A speed is a number in a unit -- um/s means
                    # nothing to a field sweep -- so it is copied only when
                    # the units match. A ROW TIME is seconds per row whatever
                    # the knob, so it always fits.
                    self.row_time.setValue(src.row_time.value())
                    mode = src.pace()
                    su, tu = src.speed_unit(), self.speed_unit()
                    if mode == "row_time":
                        self.set_pace("row_time")
                        copied.append(f"row time {src.row_time.value():g} s")
                    elif mode == "default":
                        if self.pace_box.findData("default") >= 0:
                            self.set_pace("default")
                            copied.append("the module's default rate")
                        else:
                            skipped.append("the module's default rate (this axis "
                                           "has none)")
                    elif su == tu:
                        self.set_pace("speed")
                        self.speed.setValue(src.speed.value())
                        copied.append(f"speed {src.speed.value():g} {tu}")
                    else:
                        skipped.append(f"speed ({src.speed.value():g} {su} does not "
                                       f"fit an axis in {tu})")
                    self.lag_box.setChecked(src.lag_box.isChecked())
                    self.collapse_box.setChecked(src.collapse_box.isChecked())
                    self.keep_box.setChecked(src.keep_box.isChecked())
                    if src.collapse_box.isChecked():
                        copied.append("one mean per row")
                    self.timeout_auto.setChecked(src.timeout_auto.isChecked())
                    self.timeout_spin.setValue(src.timeout_spin.value())
                    mv = src.move_param()
                    if mv and mv in self.move_choices:
                        self.move_box.setCurrentIndex(self.move_box.findData(mv))
                        copied.append(f"move with {mv}")
                    elif mv:
                        skipped.append(f"move with {mv} (not offered here)")
            elif self.is_fly():
                self.fly.setChecked(False)
                copied.append("fly off")
        elif src.is_fly():
            skipped.append("fly (a raster / zip row cannot fly)")
        # SCOUT
        if src.is_scout():
            self.scout.setChecked(True)
            self.scout_step.setValue(src.scout_step.value())
            self.margin_auto.setChecked(src.margin_auto.isChecked())
            self.margin_spin.setValue(src.margin_spin.value())
            self._margin_edited()
            m = src._ui_margin()
            copied.append(f"scout every {src.scout_step.value()}, margin "
                          + ("auto" if m == "auto" else f"{m:g} pts"))
        elif self.is_scout():
            self.scout.setChecked(False)
            copied.append("scout off")
        msg = (f"{self.param.label}: copied from {src.param.label}: "
               + (", ".join(copied) if copied else "nothing (same settings)"))
        if skipped:
            msg += "; skipped: " + ", ".join(skipped)
        self._say(msg)
        return msg

    def _say(self, msg: str) -> None:
        self.adv_note.setText(msg.split(": ", 1)[-1])
        self.log.emit(msg)

    def reset_advanced(self) -> None:
        """Back to plain stepping."""
        widgets = (self.fly, self.speed, self.row_time, self.pace_box,
                   self.lag_box, self.timeout_auto, self.collapse_box, self.keep_box,
                   self.timeout_spin, self.readback_box, self.scout,
                   self.scout_step, self.margin_auto, self.margin_spin,
                   self.name_edit)
        for w in widgets:
            w.blockSignals(True)
        try:
            self.fly.setChecked(False)
            self.speed.setValue(self._default_speed)
            self.row_time.setValue(60.0)
            self.pace_box.setCurrentIndex(0)          # speed
            self.lag_box.setChecked(True)
            self.collapse_box.setChecked(False)
            self.keep_box.setChecked(True)
            self.timeout_auto.setChecked(True)
            self.timeout_spin.setValue(120.0)
            self.readback_box.setCurrentIndex(0)
            self.scout.setChecked(False)
            self.scout_step.setValue(3)
            self.margin_auto.setChecked(True)
            self.margin_spin.setValue(2.0)
            self.name_edit.clear()
        finally:
            for w in widgets:
                w.blockSignals(False)
        if self.move_choices:
            self.move_box.setCurrentIndex(0)      # refills the knobs
        else:
            self._fill_knobs(self._default_knob)
        self._loaded_margins = None
        self.scout_note.hide()
        self._fly_toggled(False)
        self._scout_toggled(False)
        self._say(f"{self.param.label}: reset to plain stepping")

    # ---- tags ------------------------------------------------------------------
    def tag_texts(self) -> list[str]:
        out = []
        if self.is_fly():
            mode = self.pace()
            if mode == "row_time":
                t = f"fly {self.row_time.value():g} s/row"
            elif mode == "default":
                t = f"fly {self.ramp.rate_default:g} {self.speed_unit()} (default)"
            else:
                t = f"fly {self.speed.value():g} {self.speed_unit()}"
            if self.binned_by() == "command":
                # say it on the row: a map binned by what was SENT is right
                # only as far as the instrument followed its commands
                t += " (by command)"
            out.append(t)
            if self.move_param():
                out.append(f"moves {self.move_param()}")
            if self._zigzag:
                out.append("zig-zag")
            if not self.lag_box.isChecked():
                out.append("no lag correction")
            if self.collapse_box.isChecked():
                out.append("row mean" if self.keep_box.isChecked()
                           else "row mean only")
            if not self.timeout_auto.isChecked():
                out.append(f"row max {self.timeout_spin.value():g} s")
            if self.readback_box.currentData():
                out.append(f"readback {self.readback_box.currentData()}")
            knob = self.speed_param_id()
            default = (self._speed_lookup(self.move_param()) if self.move_param()
                       else None)
            default = default.id if default is not None else self._default_knob
            if knob != default and not self.is_ramp():
                out.append(f"speed knob {knob or 'none'}")
        if self.is_scout():
            t = f"scout x{self.scout_step.value()}"
            m = set(str(v) for v in self.scout_margins().values())
            if self._loaded_margins is not None and len(m) > 1:
                t += ", margin per axis"
            elif not self.margin_auto.isChecked():
                t += f", margin {self.margin_spin.value():g}"
            out.append(t)
        if self._raw is None and self.dim_name() and self.dim_name() != self.param.id:
            out.append(f"name {self.dim_name()}")
        return out

    # ---- the plain row -----------------------------------------------------------
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
        if is_bool_param(self.param):
            self.limits_lbl.setText(f"{owner}on / off")
        elif math.isfinite(lo) and math.isfinite(hi):
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

    def refresh_level(self):
        """Show the loop depth: the number, and the row's own indentation."""
        level = self._level_getter(self)
        self.level_lbl.setText(str(level))
        self._indent(level)
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

    def to_axis(self) -> dict:
        if self._raw is not None:                # loaded raster/zip: pass through
            return self._raw
        if self.is_fly():
            ax = {"type": "fly", "param": self.param.id,
                  "start": self.start.value(), "stop": self.stop.value(),
                  "num": self.num.value()}
            # exactly ONE of speed / row_time_s -- or neither: the module's
            # default rate (flyscan.fly_rate reads them in that order)
            mode = self.pace()
            if mode == "speed":
                ax["speed"] = self.speed.value()
            elif mode == "row_time":
                ax["row_time_s"] = float(self.row_time.value())
            if self.move_param():
                ax["move"] = self.move_param()
            # A speed knob only on the STAGE path: on a knob with a ramp block
            # a `speed_param` would make the engine fly it as a stage
            if self.speed_param_id() is not None and not self.is_ramp():
                ax["speed_param"] = self.speed_param_id()
            if self.readback_box.currentData():
                ax["readback"] = self.readback_box.currentData()
            if not self.lag_box.isChecked():
                ax["lag_correction"] = False
            # only what differs from the default is written, as for the rest
            if self.collapse_box.isChecked():
                ax["collapse"] = "mean"
                if not self.keep_box.isChecked():
                    ax["collapse_keep_pixels"] = False
            if not self.timeout_auto.isChecked():
                ax["timeout_s"] = float(self.timeout_spin.value())
        else:
            ax = {"type": "linear", "param": self.param.id,
                  "start": self.start.value(), "stop": self.stop.value(),
                  "num": self.num.value()}
        if self.dim_name() and self.dim_name() != self.param.id:
            ax["name"] = self.dim_name()
        for k, v in self.extra.items():
            ax.setdefault(k, v)
        return ax

    def load_axis(self, ax: dict) -> list[str]:
        """Fill the row from a loaded linear / fly axis. Returns ids this
        registry does not have (a speed knob, a 'move with' stage, a
        readback)."""
        missing = []
        if "start" in ax: self.start.setValue(float(ax["start"]))
        if "stop" in ax: self.stop.setValue(float(ax["stop"]))
        if ax.get("num"): self.num.setValue(int(ax["num"]))
        self.extra = {k: v for k, v in ax.items() if k not in self.MODELLED}
        if ax.get("name") and ax["name"] != self.param.id:
            self.name_edit.setText(str(ax["name"]))
        if ax.get("type") != "fly":
            return missing
        self.fly.setChecked(True)
        if ax.get("move"):
            i = self.move_box.findData(ax["move"])
            if i >= 0:
                self.move_box.setCurrentIndex(i)
            else:
                missing.append(ax["move"])
        sp = ax.get("speed_param")
        if sp and self.is_ramp():
            # a knob with a ramp block, flown as a STAGE by name: this row
            # has no box for that (a ramp has no speed knob), so the key rides
            # along unchanged -- and the engine still takes the stage path
            self.extra["speed_param"] = sp
            sp = None
        if sp and (self._registry is None or self._registry.get(sp) is None):
            missing.append(sp)
        self.set_speed_param(sp or None)
        # The pace: speed, else row_time_s, else (a ramp) the module default.
        # Read as written, so what was loaded is what is saved.
        if ax.get("speed") is not None:
            self.set_pace("speed")
            self.speed.setValue(float(ax["speed"]))
        elif ax.get("row_time_s") is not None:
            self.set_pace("row_time")
            self.row_time.setValue(float(ax["row_time_s"]))
        elif self.pace_box.findData("default") >= 0:
            self.set_pace("default")
        rb = ax.get("readback")
        if rb and rb != self.param.id:
            if self.readback_box.findData(rb) < 0:
                self.readback_box.addItem(f"(missing) {rb}" if self._registry is None
                                          or self._registry.get(rb) is None else rb, rb)
                if self._registry is None or self._registry.get(rb) is None:
                    missing.append(rb)
            self.readback_box.setCurrentIndex(self.readback_box.findData(rb))
        self.lag_box.setChecked(ax.get("lag_correction", True) is not False)
        self.collapse_box.setChecked(ax.get("collapse") == "mean")
        self.keep_box.setChecked(ax.get("collapse_keep_pixels") is not False)
        if ax.get("timeout_s"):
            self.timeout_auto.setChecked(False)
            self.timeout_spin.setValue(float(ax["timeout_s"]))
        return missing


class _RepeatParam:
    """What an axis row's `param` provides (id, label, unit, limits), for the
    REPEAT row, which drives no parameter -- so the code that walks the axis
    stack (tooltips, previews, the summary) needs no special case."""
    id = "repeat"
    label = "Repeat"
    unit = ""
    limits = (float("-inf"), float("inf"))


class RepeatRow(_StackRow):
    """An axis row that sets NOTHING: everything inside it is done N times
    (scan_core/repeat.py). Where it sits in the stack decides what repeats:
    on top = whole scans, at the bottom = every point N times in a row.

    `mode` keep stores every repeat as a dimension (the viewer can show one or
    average them); average stores only the mean, its spread and the count.
    `interval` (0 = none) starts repeat k no earlier than k x interval after
    the first -- a time series.

    Its Advanced panel shows only what applies to a repeat: its name in the
    file and the routines bound to it (it cannot fly or be scouted).
    """

    def __init__(self, level_getter, num: int = 5, mode: str = "keep",
                 interval_s: float | None = None, name: str | None = None):
        super().__init__()
        self.param = _RepeatParam()
        self.raw = None
        self._level_getter = level_getter
        lay = self._start_layout()

        namebox = QtWidgets.QVBoxLayout(); namebox.setSpacing(0)
        title = QtWidgets.QLabel("Repeat")
        title.setStyleSheet("font-weight:700;"); title.setFixedWidth(164)
        namebox.addWidget(title)
        self.what_lbl = QtWidgets.QLabel()
        self.what_lbl.setStyleSheet(f"color:{C['muted']}; font-size:10px;")
        self.what_lbl.setFixedWidth(164)
        namebox.addWidget(self.what_lbl)
        lay.addLayout(namebox)

        self.num = QtWidgets.QSpinBox(); self.num.setRange(1, 100000)
        self.num.setValue(int(num)); self.num.setFixedWidth(84)
        self.num.setToolTip("how many times everything inside this row is done")
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("keep all", "keep")
        self.mode.addItem("average", "average")
        self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        self.mode.setToolTip(
            "keep all: every repeat is in the file (a 'repeat' dimension); the\n"
            "viewer shows one run, or averages over them. Nothing is lost, so\n"
            "drift or one spoiled run is still visible afterwards.\n\n"
            "average: only the mean, its spread (_std) and how many repeats\n"
            "were averaged (_n) are stored -- a file N times smaller. Numbers\n"
            "only (not with a state/text detector or the window). With a fly\n"
            "axis every repeat flies the rows again and each pixel pools the\n"
            "samples of all repeats (_n = samples, _std = their spread).")
        self.interval = QtWidgets.QDoubleSpinBox()
        self.interval.setRange(0.0, 1e6); self.interval.setDecimals(1)
        self.interval.setSuffix(" s"); self.interval.setFixedWidth(96)
        self.interval.setSpecialValueText("none")
        self.interval.setValue(float(interval_s or 0.0))
        self.interval.setToolTip(
            "Optional: repeat k starts no earlier than k x this after the first\n"
            "(a time series). A repeat that takes longer starts the next at once.")
        for w, t in ((self.num, "times"), (self.mode, "store"),
                     (self.interval, "every")):
            box = QtWidgets.QVBoxLayout(); box.setSpacing(0)
            tl = QtWidgets.QLabel(t); tl.setStyleSheet(f"color:{C['muted']}; font-size:10px;")
            box.addWidget(tl); box.addWidget(w); lay.addLayout(box)
        self.num.valueChanged.connect(lambda *_: self.changed.emit())
        self.mode.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.interval.valueChanged.connect(lambda *_: self.changed.emit())

        self._finish_line(lay)
        self._point_group(with_name=True)
        self.name_edit.setPlaceholderText("repeat")
        if name:
            self.name_edit.setText(str(name))
        self.reset_btn = QtWidgets.QPushButton("Reset")
        self.reset_btn.setFixedWidth(112)
        self.reset_btn.setToolTip("Clear the name (the file then calls it 'repeat').")
        self.reset_btn.clicked.connect(self.reset_advanced)
        self.advanced.side.addWidget(self.reset_btn)
        self.advanced.side.addWidget(self.adv_note)
        self.advanced.side.addStretch(1)
        self.changed.connect(self.refresh_tags)
        self.refresh_tags()

    @property
    def name(self) -> str | None:
        return self.dim_name() or None

    @name.setter
    def name(self, value) -> None:
        self.name_edit.setText(str(value or ""))

    def reset_advanced(self) -> None:
        self.name_edit.clear()
        self.adv_note.setText("name cleared")
        self.changed.emit()

    def tag_texts(self) -> list[str]:
        return [f"name {self.dim_name()}"] if self.dim_name() else []

    # the axis-row interface the builder uses
    def is_fly(self) -> bool:
        return False

    def is_scout(self) -> bool:
        return False                       # a repeat moves nothing to scout

    def scout_dims(self) -> list[str]:
        return []

    def scout_margins(self) -> dict:
        return {}

    def set_zigzag(self, on: bool) -> None:
        pass                               # nothing to fly

    def refresh_limits(self):
        pass                               # no parameter, no limits

    def refresh_level(self):
        level = self._level_getter(self)
        self.level_lbl.setText(str(level))
        self._indent(level)
        getter = getattr(self._level_getter, "__self__", None)
        rows = list(getattr(getter, "rows", []) or [])
        inner = rows[level + 1:] if self in rows else []
        # what is repeated, in words: the thing an operator gets wrong
        if not inner:
            what = "each point, N times in a row"
        elif level == 0:
            what = "the whole scan, N times"
        else:
            what = "each sweep of " + ", ".join(r.param.label for r in inner)
        self.what_lbl.setText(what)
        self.setToolTip(f"loop level {level} -- repeats {what}")

    def to_axis(self) -> dict:
        ax = {"type": "repeat", "num": int(self.num.value()),
              "mode": self.mode.currentData()}
        if self.interval.value() > 0:
            ax["interval_s"] = float(self.interval.value())
        if self.name:
            ax["name"] = self.name
        return ax


# ──────────────────────────── one condition row ───────────────────────────────

def detector_shape(p) -> tuple[str, str]:
    """("0D" | "1D · 11101" | "2D" ..., tooltip): what ONE scan point of
    detector `p` records. The inner axes are the ones the INSTRUMENT sweeps
    itself (a spectrum's frequencies, a camera image's pixels); each becomes
    a dimension of the data on top of the scan's own."""
    axes = list(getattr(p, "axes", ()) or ())
    kind = getattr(p, "dtype", "float")
    what = {"complex": "complex numbers", "bool": "on / off", "enum": "one of a list",
            "string": "text", "text": "text", "int": "whole numbers"}.get(kind, "numbers")
    if not axes:
        return "0D", f"0D: one value per scan point ({what})"
    lens = [getattr(a, "length", None) for a in axes]
    tag = f"{len(axes)}D"
    if all(lens):
        tag += " · " + " x ".join(str(n) for n in lens)
    names = ", ".join(
        f"{getattr(a, 'label', '') or a.name}"
        + (f" [{a.unit}]" if getattr(a, "unit", "") else "")
        + (f" ({n} points)" if n else "")
        for a, n in zip(axes, lens))
    return tag, (f"{len(axes)}D: the instrument records a whole "
                 f"{'trace' if len(axes) == 1 else 'array'} at every scan point "
                 f"({what}) along {names}. The data gets these axes on top of "
                 f"the scan's own.")


def is_bool_param(param) -> bool:
    """A switch (RF output, an enable line): declared bool in its describe."""
    st = getattr(param, "storage", None)
    return getattr(st, "kind", "") == "bool"


class BoolBox(QtWidgets.QCheckBox):
    """An on/off box with the number box's interface (setValue / value /
    valueChanged / setRange ...), so a condition or routine step on a SWITCH
    reads "on" / "off" instead of 1.000 / 0.000 (Lukas 2026-10-06: "why do i
    have 1 and 0 for rf generator state?"). The value stays 1 / 0 in the
    definition; the module receives a real true / false (manifest.py)."""

    valueChanged = QtCore.Signal(float)

    def __init__(self):
        super().__init__("off")
        self.toggled.connect(self._toggled)

    def _toggled(self, on: bool):
        self.setText("on" if on else "off")
        self.valueChanged.emit(1.0 if on else 0.0)

    def setValue(self, v) -> None:                  # noqa: N802 (Qt names)
        self.setChecked(float(v) >= 0.5)

    def value(self) -> float:
        return 1.0 if self.isChecked() else 0.0

    # the number box's knobs, meaningless for a switch
    def setRange(self, *_):                         # noqa: N802
        pass

    def setDecimals(self, *_):                      # noqa: N802
        pass

    def setSuffix(self, *_):                        # noqa: N802
        pass


def _value_box(param):
    return BoolBox() if is_bool_param(param) else QtWidgets.QDoubleSpinBox()


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
        self.value_box = _value_box(param)
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
        if is_bool_param(self.param):
            return int(v)                  # 0 / 1, as the definition has always held it
        return int(round(v)) if self.integer else v


# ─────────────────────────────── routines ─────────────────────────────────────

#: The two moments the ROUTINES card edits, in the order they happen.
ROUTINE_MOMENTS = (("before_scan", "BEFORE SCAN"), ("after_scan", "AFTER SCAN"))

#: The label of the first entry of a routine's "add an action" combo. Picking
#: any OTHER entry appends that action as a step and snaps back to this one.
ADD_ACTION = "＋ run an action ..."


def _step_frame(row) -> QtWidgets.QHBoxLayout:
    """The shared left end of a routine step: a compact row and its NUMBER.

    Compact on purpose (small margins and spacing): in the suite the three
    routine columns share the card's width, ~350 px each, and a step wider
    than its column has its buttons cut off (the scroll area has no
    horizontal bar).
    """
    row.setObjectName("axis")
    lay = QtWidgets.QHBoxLayout(row)
    lay.setContentsMargins(8, 3, 6, 3); lay.setSpacing(4)
    row.marker = QtWidgets.QLabel("")
    row.marker.setStyleSheet(f"color:{C['accent']}; font-weight:800;")
    row.marker.setFixedWidth(16)
    lay.addWidget(row.marker)
    return lay


def _step_buttons(row, lay) -> None:
    """up / down / remove at the right end of a routine step.

    The order of the steps IS the order they run in, so moving a step is how
    "save the picture AFTER the focus" is said.
    """
    row.up_btn = QtWidgets.QPushButton("↑")
    row.down_btn = QtWidgets.QPushButton("↓")
    for b, delta, tip in ((row.up_btn, -1, "Run this step earlier"),
                          (row.down_btn, +1, "Run this step later")):
        b.setFixedWidth(22)
        # The suite's button padding would leave no room for the arrow in 22 px.
        b.setStyleSheet("padding: 0px;")
        b.setToolTip(tip)
        b.clicked.connect(lambda _=False, d=delta: row.move.emit(row, d))
        lay.addWidget(b)
    rm = QtWidgets.QPushButton("✕"); rm.setObjectName("danger"); rm.setFixedWidth(24)
    rm.setStyleSheet("padding: 0px;")
    rm.setToolTip("Remove this step")
    rm.clicked.connect(lambda: row.remove.emit(row))
    lay.addWidget(rm)


class SetStepRow(FixedRow):
    """A routine step "set <parameter> = value".

    Behaves as a FixedRow (the live limits under the name, the value clamped
    to them, re-clamped by refresh_limits), because a routine's set IS a
    setpoint -- held for a moment instead of for the whole scan -- and it is
    checked exactly like one. Only the layout differs: narrower (the unit sits
    in the value box), a step number instead of "=", and up / down buttons.
    """

    move = QtCore.Signal(object, int)

    def __init__(self, param, value: float | None = None):
        QtWidgets.QFrame.__init__(self)       # FixedRow's layout is not wanted
        self.param = param
        lay = _step_frame(self)

        namebox = QtWidgets.QVBoxLayout(); namebox.setSpacing(0)
        name = QtWidgets.QLabel(param.label)
        name.setStyleSheet("font-weight:700;")
        name.setToolTip(param.id)
        self.limits_lbl = QtWidgets.QLabel()
        self.limits_lbl.setStyleSheet(f"color:{C['muted']}; font-size:10px;")
        for w in (name, self.limits_lbl):
            # May be CUT (full text in the tooltip), never widen the column.
            w.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            w.setMinimumWidth(96)
            namebox.addWidget(w)
        lay.addLayout(namebox, 1)

        self.integer = bool(getattr(param, "integer", False))
        lo, hi = self._finite_limits()
        self.value_box = _value_box(param)
        self.value_box.setRange(lo, hi)
        self.value_box.setDecimals(0 if self.integer else 3)
        if param.unit:
            self.value_box.setSuffix(f" {param.unit}")
        self.value_box.setFixedWidth(104)
        start = value if value is not None else self._default_value(lo, hi)
        self.value_box.setValue(float(start))
        self.value_box.valueChanged.connect(lambda *_: self.changed.emit())
        lay.addWidget(self.value_box)
        _step_buttons(self, lay)
        self._sync_limits_label()
        self.limits_lbl.setToolTip(self.limits_lbl.text())

    def refresh_limits(self):
        super().refresh_limits()
        self.limits_lbl.setToolTip(self.limits_lbl.text())

    def set_number(self, k: int) -> None:
        self.marker.setText(str(k))

    def to_step(self) -> dict:
        return {"set": {self.param.id: self.value()}}

    def text(self) -> str:
        if is_bool_param(self.param):
            return f"{self.param.label} {'on' if self.value() else 'off'}"
        unit = f" {self.param.unit}" if self.param.unit else ""
        return f"{self.param.label} = {self.value():g}{unit}"


class ActionArgsPanel(QtWidgets.QFrame):
    """The ADVANCED options of a routine step that runs an action: one line
    per argument the module declares (describe `args`), built from that list
    alone -- type, unit, min / max, options, default, help -- so ANY module's
    action with arguments gets it, not only the camera's.

    Each line has a tick box: TICKED = this value is sent with the action,
    unticked = not sent, and the module uses its OWN setting (the camera's
    configured AF position, its autofocus settings, ...). That is the point of
    the options: a routine overrides only what it means to, and a module
    setting changed later still counts for everything the routine left alone.
    """

    changed = QtCore.Signal()

    def __init__(self, specs: list, values: dict | None = None):
        super().__init__()
        self.setObjectName("actionArgs")
        self.setStyleSheet(
            f"QFrame#actionArgs {{ border-top: 1px dashed {C['border']}; }}")
        values = dict(values or {})
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(24, 4, 6, 4)
        grid.setHorizontalSpacing(6); grid.setVerticalSpacing(2)
        # the NAME takes the free room (cut, never widening the ~350 px
        # column); the editors keep one fixed width, so they line up
        grid.setColumnStretch(0, 1)
        #: name -> (tick box, editor, spec)
        self.lines: dict = {}
        for r, spec in enumerate(specs):
            name = spec["name"]
            tick = QtWidgets.QCheckBox(spec.get("label") or name)
            tip = (spec.get("help") or "").strip()
            tick.setToolTip((tip + "\n" if tip else "") + f"argument '{name}'. Ticked = sent "
                            f"with the action; unticked = the module's own setting.")
            tick.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            tick.setMinimumWidth(90)
            editor = self._editor(spec, values.get(name))
            editor.setToolTip(tick.toolTip())
            editor.setFixedWidth(120)
            tick.setChecked(name in values)
            editor.setEnabled(name in values)
            tick.toggled.connect(lambda on, e=editor: (e.setEnabled(on), self.changed.emit()))
            grid.addWidget(tick, r, 0)
            grid.addWidget(editor, r, 1)
            self.lines[name] = (tick, editor, spec)
        # values the module no longer declares are KEPT (and shown as tags),
        # so a definition loaded next to an older module comes back unchanged;
        # recipe.validate() names them before a run
        self.extra = {k: v for k, v in values.items() if k not in self.lines}

    def _editor(self, spec: dict, value):
        t = spec.get("type", "float")
        lo, hi = spec.get("min"), spec.get("max")
        default = spec.get("default")
        if t == "bool":
            w = QtWidgets.QCheckBox("on")
            w.setChecked(bool(value if value is not None else default))
            w.toggled.connect(lambda on, w=w: (w.setText("on" if on else "off"),
                                               self.changed.emit()))
            w.setText("on" if w.isChecked() else "off")
            return w
        if t == "enum":
            w = QtWidgets.QComboBox()
            for o in spec.get("options") or []:
                w.addItem(str(o))
            pick = value if value is not None else default
            if pick is not None and w.findText(str(pick)) >= 0:
                w.setCurrentText(str(pick))
            w.currentTextChanged.connect(lambda *_: self.changed.emit())
            return w
        if t == "string":
            w = QtWidgets.QLineEdit("" if value is None and default is None
                                    else str(value if value is not None else default))
            w.textChanged.connect(lambda *_: self.changed.emit())
            return w
        if t == "int":
            w = QtWidgets.QSpinBox()
            w.setRange(int(lo) if lo is not None else -2**31,
                       int(hi) if hi is not None else 2**31 - 1)
        else:
            w = QtWidgets.QDoubleSpinBox()
            w.setDecimals(4)
            w.setRange(float(lo) if lo is not None else -1e12,
                       float(hi) if hi is not None else 1e12)
        if spec.get("unit"):
            w.setSuffix(f" {spec['unit']}")
        start = value if value is not None else default
        if start is None:
            # nothing given: 0 when allowed, else the nearest limit -- visibly
            # a starting point to type over (the routine's set steps do the same)
            start = 0 if (lo is None or lo <= 0) and (hi is None or hi >= 0) else \
                (lo if lo is not None else hi)
        w.setValue(int(round(float(start))) if t == "int" else float(start))
        w.valueChanged.connect(lambda *_: self.changed.emit())
        return w

    def values(self) -> dict:
        """{name: value} of the TICKED lines (+ kept unknown ones), in declared order."""
        out = {}
        for name, (tick, w, spec) in self.lines.items():
            if not tick.isChecked():
                continue
            t = spec.get("type", "float")
            if t == "bool":
                out[name] = bool(w.isChecked())
            elif t == "enum":
                out[name] = w.currentText()
            elif t == "string":
                out[name] = w.text()
            elif t == "int":
                out[name] = int(w.value())
            else:
                out[name] = float(w.value())
        out.update(self.extra)
        return out


class ActionStepRow(QtWidgets.QFrame):
    """A routine step "run <action>" -- one registry action, waited for.

    The scan does not go on to the next step until the action has finished
    (an autofocus has parked, a reference sweep is in), which is what makes
    "find focus, then save the pattern, then save a picture" safe to write
    down as three steps.

    An action that declares ARGUMENTS (2026-10-10, Lukas: the AF position and
    the autofocus's settings "are advanced settings in the procedure called in
    before/after/throughout scan") gets a gear button: it opens the step's
    Advanced options IN PLACE under the step (ActionArgsPanel). Whatever is
    set there is saved in the recipe as the step's `args`, sent with the
    action, and shown as small tags on the step, so a closed step still says
    what it will do differently.
    """

    remove = QtCore.Signal(object)
    move = QtCore.Signal(object, int)
    changed = QtCore.Signal()
    #: the step changed HEIGHT (Advanced opened / closed, tags appeared): a
    #: section that sizes its step list to the steps must measure again
    resized = QtCore.Signal()

    def __init__(self, action, args: dict | None = None):
        super().__init__()
        self.aid = action.id
        self.setObjectName("axis")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        top = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(top)
        lay.setContentsMargins(8, 3, 6, 3); lay.setSpacing(4)     # as _step_frame
        self.marker = QtWidgets.QLabel("")
        self.marker.setStyleSheet(f"color:{C['accent']}; font-weight:800;")
        self.marker.setFixedWidth(16)
        lay.addWidget(self.marker)
        run = QtWidgets.QLabel("run")
        run.setStyleSheet(f"color:{C['muted']};")
        lay.addWidget(run)
        namebox = QtWidgets.QVBoxLayout(); namebox.setSpacing(0)
        name = QtWidgets.QLabel(action.label)
        name.setStyleSheet("font-weight:700;")
        ident = QtWidgets.QLabel(action.id)
        ident.setStyleSheet(f"color:{C['muted']}; font-size:10px;")
        for w in (name, ident):
            w.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            w.setMinimumWidth(60)
            w.setToolTip(f"{action.id}\n{action.help}" if action.help else action.id)
            namebox.addWidget(w)
        # the tags: one per argument the step SETS (clipped, never widening
        # the column; the full list is in the tooltip)
        self.tags = QtWidgets.QWidget()
        self.tags.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                QtWidgets.QSizePolicy.Preferred)
        self.tags_box = QtWidgets.QHBoxLayout(self.tags)
        self.tags_box.setContentsMargins(0, 1, 0, 0); self.tags_box.setSpacing(3)
        self.tags_box.addStretch(1)
        namebox.addWidget(self.tags)
        lay.addLayout(namebox, 1)
        specs = list(getattr(action, "arg_specs", None) or [])
        self.args_panel = None
        self.adv_btn = None
        if specs or args:
            self.adv_btn = QtWidgets.QPushButton()
            self.adv_btn.setIcon(_gear_icon())
            self.adv_btn.setCheckable(True)
            self.adv_btn.setFixedWidth(26)
            self.adv_btn.setStyleSheet("padding: 0px;")
            self.adv_btn.setToolTip("Advanced: this action's own options for THIS step\n"
                                    "(ticked = sent; unticked = the module's setting).")
            self.adv_btn.toggled.connect(self.set_advanced_open)
            lay.addWidget(self.adv_btn)
        _step_buttons(self, lay)
        outer.addWidget(top)
        if self.adv_btn is not None:
            self.args_panel = ActionArgsPanel(specs, args)
            self.args_panel.setVisible(False)
            self.args_panel.changed.connect(self._args_changed)
            outer.addWidget(self.args_panel)
        self._sync_tags()

    def set_advanced_open(self, on: bool) -> None:
        if self.args_panel is None:
            return
        self.adv_btn.blockSignals(True)
        self.adv_btn.setChecked(on)
        self.adv_btn.blockSignals(False)
        self.args_panel.setVisible(on)
        self.resized.emit()

    def advanced_open(self) -> bool:
        return self.args_panel is not None and self.args_panel.isVisibleTo(self)

    def args(self) -> dict:
        """The arguments this step sends ({} = none: the module's defaults)."""
        return self.args_panel.values() if self.args_panel is not None else {}

    def _args_changed(self) -> None:
        self._sync_tags()
        self.changed.emit()

    def _sync_tags(self) -> None:
        from scan_core.hooks import args_text
        while self.tags_box.count() > 1:
            item = self.tags_box.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(None)
        vals = self.args()
        for k, v in vals.items():
            self.tags_box.insertWidget(self.tags_box.count() - 1,
                                       _axis_tag(args_text({k: v}), args_text(vals)))
        self.tags.setVisible(bool(vals))
        self.tags.setToolTip(args_text(vals))
        self.resized.emit()

    def set_number(self, k: int) -> None:
        self.marker.setText(str(k))

    def to_step(self) -> dict:
        a = self.args()
        return {"action": self.aid, "args": a} if a else {"action": self.aid}

    def text(self) -> str:
        from scan_core.hooks import args_text
        a = self.args()
        return f"{self.aid} ({args_text(a)})" if a else self.aid

    def refresh_limits(self) -> None:          # an action has none
        pass


#: The first entry of a routine's "add another kind of step" combo, and the
#: kinds it offers (hooks.STEP_KINDS): label, kind.
ADD_STEP = "＋ other step ..."
STEP_CHOICES = (("wait until ...", "wait_until"),
                ("abort scan if ...", "abort_if"),
                ("skip point if ...", "skip_if"),
                ("pause for the operator", "pause"),
                ("comment into the file", "comment"),
                ("set from a formula", "compute_set"))

#: The short word at the left of each generic step (the columns are narrow).
STEP_TAGS = {"wait_until": "wait until", "abort_if": "abort if", "skip_if": "skip if",
             "pause": "pause:", "comment": "comment:", "compute_set": "set"}

#: What each generic step does, for its tooltip.
STEP_HELP = {
    "wait_until": "Wait until the condition has been TRUE without a break for\n"
                  "'hold' seconds (0 = true once). Looked at every 0.5 s from\n"
                  "the status (no new acquisitions). After 'max' seconds: stop\n"
                  "the scan (data kept, reason in the file) or carry on.",
    "abort_if": "If the condition is true, STOP the scan here -- like Abort:\n"
                "the after-scan routine runs, the data so far is saved, and\n"
                "the file says why (attribute stopped_by).",
    "skip_if": "If the condition is true, leave THIS point out (stored as not\n"
               "measured, NaN) and go on with the next one. Before the point:\n"
               "it is not measured at all; after it: the values are discarded.",
    "pause": "Wait for you: a banner shows the message with Continue and\n"
             "Abort scan. A run without this window (a script) fails here,\n"
             "or -- 'no GUI: carry on' -- only logs the message.",
    "comment": "Add a timestamped line to the file's comment log (attribute\n"
               "comments). {parameter.id} is replaced by its current value.",
    "compute_set": "Set a parameter to the value of a formula (the same blocking\n"
                   "set as a plain set, and restored the same way). A value\n"
                   "outside the parameter's limits makes the step FAIL -- it is\n"
                   "never clamped.",
}

#: The defaults of the optional keys: a step writes such a key back only when
#: it was in the file or differs from this, so a recipe round-trips unchanged.
STEP_DEFAULTS = {"wait_until": {"hold_s": 0.0, "on_timeout": "stop"},
                 "abort_if": {"scope": "scan"},
                 "pause": {"headless": "fail"}}


class GenericStepRow(QtWidgets.QFrame):
    """A routine step of one of the five generic kinds (hooks.STEP_KINDS):
    wait until / abort if / skip if / pause / comment / set from a formula.

    One small form per kind, in the same compact frame as a set or an action
    step (number on the left, up / down / remove on the right). A condition or
    formula is checked AS YOU TYPE with the same function recipe.validate()
    uses (expr.check, against the registry's ids): a red border and the reason
    underneath when it would be refused, so a typo is seen in the builder and
    not when the scan reaches it at 3 a.m.
    """

    remove = QtCore.Signal(object)
    move = QtCore.Signal(object, int)
    changed = QtCore.Signal()

    def __init__(self, kind: str, spec: dict | None, registry):
        super().__init__()
        self.kind = kind
        self.registry = registry
        self._orig = dict(spec or {})          # what was loaded (key presence)
        lay = _step_frame(self)
        body = QtWidgets.QVBoxLayout(); body.setSpacing(2)
        line1 = QtWidgets.QHBoxLayout(); line1.setSpacing(4)
        tag = QtWidgets.QLabel(STEP_TAGS[kind])
        tag.setStyleSheet(f"color:{C['muted']};")
        tag.setToolTip(STEP_HELP[kind])
        line1.addWidget(tag)
        body.addLayout(line1)
        line2 = QtWidgets.QHBoxLayout(); line2.setSpacing(4)
        self.cond = self.text_edit = self.param_box = None
        spec = self._orig

        def edit(text, placeholder, tip):
            w = QtWidgets.QLineEdit(str(text))
            w.setPlaceholderText(placeholder)
            w.setToolTip(tip)
            # may be narrow, never widens the column (see _step_frame)
            w.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            w.setMinimumWidth(110)
            w.textChanged.connect(lambda *_: self._changed())
            return w

        def spin(value, lo, hi, prefix, tip):
            w = QtWidgets.QDoubleSpinBox()
            w.setRange(lo, hi); w.setDecimals(1); w.setValue(float(value))
            w.setPrefix(prefix); w.setSuffix(" s"); w.setFixedWidth(108)
            w.setToolTip(tip)
            w.valueChanged.connect(lambda *_: self._changed())
            return w

        def combo(items, current, tip):
            w = QtWidgets.QComboBox()
            for label, data in items:
                w.addItem(label, data)
            w.setCurrentIndex(max(0, w.findData(current)))
            w.setToolTip(tip)
            w.currentIndexChanged.connect(lambda *_: self._changed())
            return w

        expr_tip = ("A condition: parameter ids (as in the palette, e.g.\n"
                    "ppms.temperature), numbers, + - * / **, < <= > >= == !=,\n"
                    "and / or / not, abs min max round. Values come from the\n"
                    "status -- no new measurement is taken.")
        if kind in ("wait_until", "abort_if", "skip_if"):
            self.cond = edit(spec.get("condition", ""), "condition, e.g. field > 100",
                             expr_tip)
            line1.addWidget(self.cond, 1)
        if kind == "abort_if":
            self.scope = combo((("abort scan", "scan"), ("abort all", "all")),
                               spec.get("scope") or "scan",
                               "abort scan: only this scan stops (a queue goes on\n"
                               "with the next one). abort all: this scan AND the rest\n"
                               "of the queue -- for a safety condition.")
            line1.addWidget(self.scope)
        if kind == "wait_until":
            self.hold = spin(spec.get("hold_s", 0.0), 0, 1e6, "hold ",
                             "The condition must stay true this long without a\n"
                             "break (0 = true once is enough).")
            self.timeout = spin(spec.get("timeout_s", 3600.0), 0.1, 1e7, "max ",
                                "Give up after this long (required).")
            self.on_timeout = combo((("then abort scan", "stop"), ("then abort all", "stop_all"),
                                     ("then carry on", "continue")),
                                    spec.get("on_timeout") or "stop",
                                    "After 'max': abort this scan (the data is kept, the\n"
                                    "file says why), abort all (this scan AND the rest of\n"
                                    "the queue), or log it and carry on.")
            for w in (self.hold, self.timeout, self.on_timeout):
                line2.addWidget(w)
        if kind == "pause":
            self.text_edit = edit(spec.get("message", ""), "message, e.g. Insert the polariser",
                                  "What the banner tells you to do before Continue.")
            line1.addWidget(self.text_edit, 1)
            self.headless = combo((("no GUI: fail", "fail"), ("no GUI: carry on", "continue")),
                                  spec.get("headless") or "fail",
                                  "In a run without this window (a script, the queue\n"
                                  "headless): fail with a clear message, or only log it.")
            line2.addWidget(self.headless)
        if kind == "comment":
            self.text_edit = edit(spec.get("text", ""), "text; {field} = its value",
                                  "Written with the time (and the point) into the\n"
                                  "file's comments. {parameter.id} becomes its value.")
            line1.addWidget(self.text_edit, 1)
        if kind == "compute_set":
            sets = spec.get("set") or {}
            pid, text = next(iter(sets.items())) if sets else ("", "")
            self.param_box = QtWidgets.QComboBox()
            self.param_box.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                         QtWidgets.QSizePolicy.Fixed)
            self.param_box.setMinimumWidth(90)
            for p in (registry.settables() if registry is not None else []):
                self.param_box.addItem(p.label, p.id)
                self.param_box.setItemData(self.param_box.count() - 1, p.id,
                                           QtCore.Qt.ToolTipRole)
            if pid and self.param_box.findData(pid) < 0:
                self.param_box.addItem(f"{pid} (not here)", pid)
            if pid:
                self.param_box.setCurrentIndex(self.param_box.findData(pid))
            self.param_box.currentIndexChanged.connect(lambda *_: self._changed())
            line1.addWidget(self.param_box, 1)
            eq = QtWidgets.QLabel("="); line1.addWidget(eq)
            self.cond = edit(text, "formula, e.g. 2800 + 28 * field",
                             expr_tip.replace("A condition", "A formula"))
            line2.addWidget(self.cond, 1)
        if line2.count():
            body.addLayout(line2)
        self.err_lbl = QtWidgets.QLabel("")
        self.err_lbl.setWordWrap(True)
        self.err_lbl.setStyleSheet(f"color:{C['danger']}; font-size:10px;")
        self.err_lbl.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                   QtWidgets.QSizePolicy.Preferred)
        self.err_lbl.hide()
        body.addWidget(self.err_lbl)
        lay.addLayout(body, 1)
        _step_buttons(self, lay)
        self._validate()

    # ---- live check ------------------------------------------------------------

    def problems(self) -> list[str]:
        """What validate() would say about this step's text ([] = fine)."""
        from scan_core import expr as _expr
        if self.cond is None:
            if self.kind == "pause" and not self.text_edit.text().strip():
                return ["write the message the banner should show"]
            return []
        msgs = _expr.check(self.cond.text(), self.registry)
        if self.kind == "compute_set" and not self.param_box.currentData():
            msgs.insert(0, "pick the parameter to set")
        return msgs

    def _validate(self):
        msgs = self.problems()
        if self.cond is not None:
            self.cond.setStyleSheet(
                f"QLineEdit {{ border: 1px solid {C['danger']}; }}" if msgs else "")
        self.err_lbl.setText(msgs[0] if msgs else "")
        self.err_lbl.setToolTip("\n".join(msgs))
        self.err_lbl.setVisible(bool(msgs))

    def _changed(self):
        self._validate()
        self.changed.emit()

    # ---- the RoutineSection interface -----------------------------------------

    def set_number(self, k: int) -> None:
        self.marker.setText(str(k))

    def refresh_limits(self) -> None:          # nothing to clamp
        pass

    def _keep(self, out: dict, key: str, value):
        """Write an optional key only if the file had it or it is not the
        default -- so a loaded step is written back exactly as it was."""
        if key in self._orig or value != STEP_DEFAULTS.get(self.kind, {}).get(key):
            out[key] = self._back(key, value)

    def _back(self, key: str, value):
        """The loaded value itself when it is unchanged (600, not 600.0)."""
        old = self._orig.get(key)
        if isinstance(value, float) and isinstance(old, (int, float))                 and not isinstance(old, bool) and float(old) == value:
            return old
        return value

    def spec(self) -> dict:
        k = self.kind
        if k == "abort_if":
            out = {"condition": self.cond.text()}
            self._keep(out, "scope", self.scope.currentData())
            return out
        if k == "skip_if":
            return {"condition": self.cond.text()}
        if k == "wait_until":
            out = {"condition": self.cond.text()}
            self._keep(out, "hold_s", float(self.hold.value()))
            out["timeout_s"] = self._back("timeout_s", float(self.timeout.value()))
            self._keep(out, "on_timeout", self.on_timeout.currentData())
            # the file's own key order, so a loaded step re-saves identically
            order = [key for key in self._orig if key in out]
            return {key: out[key] for key in order + [x for x in out if x not in order]}
        if k == "pause":
            out = {"message": self.text_edit.text()}
            self._keep(out, "headless", self.headless.currentData())
            return out
        if k == "comment":
            return {"text": self.text_edit.text()}
        return {"set": {self.param_box.currentData() or "": self.cond.text()}}

    def to_step(self) -> dict:
        return {self.kind: self.spec()}

    def text(self) -> str:
        s = self.spec()
        if self.kind == "wait_until":
            hold = f", hold {s.get('hold_s', 0):g} s" if s.get("hold_s") else ""
            return f"wait until {s['condition']}{hold} (max {s['timeout_s']:g} s)"
        if self.kind == "abort_if":
            return f"abort if {s['condition']}"
        if self.kind == "skip_if":
            return f"skip point if {s['condition']}"
        if self.kind == "pause":
            return f"pause: {s['message']}"
        if self.kind == "comment":
            return f"comment: {s['text']}"
        (pid, text), = s["set"].items()
        return f"{pid} = {text}"


def _generic_missing(kind: str, spec, registry) -> list[str]:
    """The ids a generic step names that this registry does not have -- so
    loading flags them like a missing axis (the step itself still loads, with
    its red border)."""
    from scan_core import expr as _expr
    if not isinstance(spec, dict) or registry is None:
        return []
    texts = []
    out = []
    if kind in ("wait_until", "abort_if", "skip_if"):
        texts.append(spec.get("condition"))
    if kind == "compute_set" and isinstance(spec.get("set"), dict):
        for pid, text in spec["set"].items():
            p = registry.get(pid)
            if p is None or getattr(p, "kind", "") != "settable":
                out.append(pid)
            texts.append(str(text))
    for t in texts:
        try:
            out += [pid for pid in _expr.parse(t).names if registry.get(pid) is None]
        except _expr.ExprError:
            pass
    return out


class RoutineSection(QtWidgets.QFrame):
    """One routine: an ORDERED list of steps, each "set <param> = value",
    "run <action>" or one of the five generic steps (GenericStepRow: wait
    until, abort if, skip point if, pause, comment, set from a formula), run
    top to bottom, every one waited for.

    Before 2026-09-25 a routine was "these sets, then ONE action". Lukas wanted
    several actions at one moment in a given order -- before the scan: find
    focus, then save the scan pattern, then save a camera picture -- so the
    steps are now a list: add, remove, move up / down. Parameters come from the
    palette ("+ Before" / "+ After" / "+ Throughout" append a set step);
    actions from the "run an action" list under the steps (it lists
    `registry.actions()`).

    It is stored in the recipe as ONE `call` hook, so a routine is just data
    like the rest of the scan -- saved in the .yaml, carried inside every .nc,
    restored on load. to_hook() writes the ORIGINAL form {set: {...}, action:
    X} whenever that says the same thing (sets, then at most one action; an
    older scan-core reads it), and the ordered form {steps: [...]} otherwise.
    One hook, not one per action, because the RESTORE (hooks._call) then runs
    once, after the last step -- see hooks.py for why that matters.
    """

    changed = QtCore.Signal()

    def __init__(self, when: str, title: str):
        super().__init__()
        self.when = when
        self.steps: list = []                 # SetStepRow | ActionStepRow, in run order
        self._actions: dict = {}              # action id -> Action the registry offers
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)

        head = QtWidgets.QHBoxLayout()
        tag = QtWidgets.QLabel(title); tag.setObjectName("tag")
        head.addWidget(tag)
        self.title_lbl = tag
        self.empty_lbl = QtWidgets.QLabel("nothing -- add parameters or actions, in order")
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
        # ONE combo instead of "combo + Add button": picking an action appends
        # it as the last step and the combo returns to its "+ run an action"
        # entry, ready for the next one. `activated` fires only on a USER pick,
        # so refilling the list never adds a step by itself.
        self.add_combo = QtWidgets.QComboBox()
        # Both combos share the row's width (3 : 2) instead of each asking for
        # its longest entry: the columns are ~350 px in the suite, and two
        # combos sized to "Autofocus (simulated)   (sim_autofocus)" pushed the
        # row past the THROUGHOUT column's edge.
        self.add_combo.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                     QtWidgets.QSizePolicy.Fixed)
        self.add_combo.setMinimumWidth(150)
        self.add_combo.activated.connect(self._picked)
        act.addWidget(self.add_combo, 3)
        # The five generic steps (2026-10-04) in a combo of their own, so the
        # action list stays exactly the registry's actions.
        self.step_combo = QtWidgets.QComboBox()
        self.step_combo.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                      QtWidgets.QSizePolicy.Fixed)
        self.step_combo.setMinimumWidth(110)
        self.step_combo.addItem(ADD_STEP, None)
        for label, kind in STEP_CHOICES:
            self.step_combo.addItem(label, kind)
            self.step_combo.setItemData(self.step_combo.count() - 1, STEP_HELP[kind],
                                        QtCore.Qt.ToolTipRole)
        self.step_combo.setToolTip("Adds a wait, a check, a pause, a comment or a\n"
                                   "computed set as the LAST step.")
        self.step_combo.activated.connect(self._picked_step)
        act.addWidget(self.step_combo, 2)
        act.setContentsMargins(0, 0, 6, 0)      # clear of the scroll bar's edge
        v.addLayout(act)
        self._registry = None
        self.set_actions([])

    # ---- the steps -------------------------------------------------------------

    def minimumSizeHint(self):
        """At least as wide as the widest step (+ the scroll bar).

        The steps sit in a scroll area, and a scroll area reports only its own
        frame as its minimum -- so without this the RoutinesCard would measure
        a column as narrow as its "run an action" list, put three of them side
        by side in the standalone builder's narrow column, and cut every
        step's buttons off.
        """
        hint = super().minimumSizeHint()
        widest = max((st.minimumSizeHint().width() for st in self.steps), default=0)
        if widest:
            sc = self.rows_scroll
            widest += sc.verticalScrollBar().sizeHint().width() + 2 * sc.frameWidth()
        return QtCore.QSize(max(hint.width(), widest), hint.height())

    @property
    def rows(self) -> list:
        """The SET steps only (what limit refreshes and older callers want)."""
        return [s for s in self.steps if isinstance(s, SetStepRow)]

    def _insert(self, row, at: int | None = None):
        row.remove.connect(self.remove_step)
        row.move.connect(self.move_step)
        if isinstance(row, (SetStepRow, GenericStepRow, ActionStepRow)):
            row.changed.connect(self._changed)
        if isinstance(row, ActionStepRow):
            row.resized.connect(self._relayout)
        at = len(self.steps) if at is None else at
        self.steps.insert(at, row)
        self.lay.insertWidget(at, row)            # the stretch stays last
        self._changed()
        return row

    def add_set(self, param, value: float | None = None,
                merge: bool = True) -> SetStepRow:
        """Append "set param = value" (the palette's + Before / After / Throughout).

        With `merge`, a parameter already set AFTER the last action is updated
        in place instead: two sets of it with nothing run in between would just
        be two setpoints, and only the last would count. After an action a new
        step is added -- "field 190, take the reference, field 0" needs both.
        Loading a file passes merge=False and reproduces it exactly.
        """
        if merge:
            for row in reversed(self.steps):
                if not isinstance(row, SetStepRow):     # an action, a wait, ...
                    break
                if row.param.id == param.id:
                    if value is not None:
                        row.value_box.setValue(float(value))
                    row.value_box.setFocus()
                    return row
        return self._insert(SetStepRow(param, value))

    def add_action(self, aid: str, args: dict | None = None) -> "ActionStepRow | None":
        """Append "run aid" (with its Advanced `args`, if any). None if no
        connected module offers that action."""
        a = self._actions.get(aid)
        if a is None:
            return None
        return self._insert(ActionStepRow(a, args))

    def set_registry(self, registry) -> None:
        """The registry a generic step checks its conditions against (and
        whose settables "set from a formula" offers)."""
        self._registry = registry
        for row in self.steps:
            if isinstance(row, GenericStepRow):
                row.registry = registry
                row._validate()

    def add_generic(self, kind: str, spec: dict | None = None) -> GenericStepRow:
        """Append one of the five generic steps (hooks.STEP_KINDS)."""
        return self._insert(GenericStepRow(kind, spec, self._registry))

    def _picked_step(self, index: int) -> None:
        kind = self.step_combo.itemData(index)
        self.step_combo.setCurrentIndex(0)
        if kind:
            row = self.add_generic(kind)
            focus = row.cond or row.text_edit
            if focus is not None:
                focus.setFocus()

    def _relayout(self) -> None:
        """A step changed height (its Advanced options opened): measure again."""
        self.updateGeometry()

    def remove_step(self, row) -> None:
        if row in self.steps:
            self.steps.remove(row)
            row.setParent(None)
            self._changed()

    remove_set = remove_step                      # the pre-2026-09-25 name

    def move_step(self, row, delta: int) -> None:
        """Move a step earlier (-1) or later (+1): the order is the run order."""
        i = self.steps.index(row); j = i + delta
        if not 0 <= j < len(self.steps):
            return
        self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
        for s in self.steps:
            self.lay.removeWidget(s)
        for k, s in enumerate(self.steps):
            self.lay.insertWidget(k, s)
        self._changed()

    def clear(self) -> None:
        for row in list(self.steps):
            self.remove_step(row)

    # ---- actions ----------------------------------------------------------

    def set_actions(self, actions) -> None:
        """Refill the "run an action" list from `registry.actions()`.

        An action step whose action is no longer offered is dropped -- as a
        set step is when its parameter goes -- and loading names it as missing.
        """
        self._actions = {a.id: a for a in actions}
        for row in [s for s in self.steps if isinstance(s, ActionStepRow)]:
            if row.aid not in self._actions:
                self.remove_step(row)
        self.add_combo.blockSignals(True)
        self.add_combo.clear()
        self.add_combo.addItem(ADD_ACTION, None)
        for a in actions:
            self.add_combo.addItem(f"{a.label}   ({a.id})", a.id)
            i = self.add_combo.count() - 1
            self.add_combo.setItemData(i, a.help or a.id, QtCore.Qt.ToolTipRole)
        self.add_combo.setCurrentIndex(0)
        # Disabled, with the reason, rather than a list offering nothing and
        # leaving the operator to guess why the reference is not there.
        self.add_combo.setEnabled(bool(actions))
        self.add_combo.setToolTip(
            "Adds the action as the LAST step. Steps run top to bottom; each\n"
            "one is waited for (a set until it has settled, an action until it\n"
            "has finished). Use the arrows to change the order."
            if actions else
            "No connected module offers an action a scan can wait on.\n"
            "(A module offers one by giving the action a `wait` block in describe.)")
        self.add_combo.blockSignals(False)
        self._changed()

    def _picked(self, index: int) -> None:
        aid = self.add_combo.itemData(index)
        self.add_combo.setCurrentIndex(0)
        if aid:
            self.add_action(aid)

    def action_ids(self) -> list[str]:
        """Every action the routine runs, in order."""
        return [s.aid for s in self.steps if isinstance(s, ActionStepRow)]

    def action_id(self) -> str | None:
        """The LAST action (the only one, for a routine in the original form)."""
        ids = self.action_ids()
        return ids[-1] if ids else None

    def set_action(self, aid: str | None) -> bool:
        """The original one-action API: make `aid` THE action, run after every
        set (None = no action). False if no module offers it. To run several
        actions, use add_action()."""
        if aid and aid not in self._actions:
            return False
        for row in [s for s in self.steps if isinstance(s, ActionStepRow)]:
            self.remove_step(row)
        if aid:
            self.add_action(aid)
        return True

    def _changed(self):
        if not hasattr(self, "add_combo"):        # still being built
            return
        for k, s in enumerate(self.steps, 1):
            s.set_number(k)
            s.up_btn.setEnabled(k > 1)
            s.down_btn.setEnabled(k < len(self.steps))
        self.empty_lbl.setVisible(not self.steps)
        self.changed.emit()

    # ---- recipe --------------------------------------------------------------

    def to_args(self) -> dict | None:
        """The `call` arguments these steps stand for; None when empty.

        The ORIGINAL form -- {set: {...}, action: X} -- when it says exactly the
        same thing: sets of different parameters, then at most one action. The
        ordered form {steps: [...]} otherwise (two actions, a set after an
        action, a parameter set twice). Both run the same way (hooks.py,
        routine_steps); the original keeps simple routines readable by an
        older scan-core and keeps existing files byte-identical on re-save.
        """
        if not self.steps:
            return None
        kinds = ["action" if isinstance(s, ActionStepRow) else
                 "set" if isinstance(s, SetStepRow) else "generic" for s in self.steps]
        n_act = kinds.count("action")
        pids = [s.param.id for s in self.rows]
        # a generic step (wait, check, pause, comment, formula) only exists in
        # the ordered form
        simple = (n_act == 0 or (n_act == 1 and kinds[-1] == "action")) \
            and len(set(pids)) == len(pids) and "generic" not in kinds
        if simple:
            args = {}
            if pids:
                args["set"] = {s.param.id: s.value() for s in self.rows}
            if n_act:
                args["action"] = self.steps[-1].aid
                if self.steps[-1].args():
                    # {set, action, args}: the action's Advanced options
                    args["args"] = self.steps[-1].args()
            return args
        return {"steps": [s.to_step() for s in self.steps]}

    def to_hook(self) -> dict | None:
        """The `call` hook this section stands for, or None when it is empty
        (an empty routine is simply not written into the recipe)."""
        args = self.to_args()
        if not args:
            return None
        return {"when": self.when, "action": "call", "args": args}

    def load_args(self, args, registry) -> list[str]:
        """Append the steps of a `call` hook's args; return the ids missing here.

        What is available loads, in order; a parameter or action this registry
        lacks is left out and NAMED, exactly like a missing axis -- a
        definition written against the lab must not come back on the simulator
        quietly skipping its reference.
        """
        from scan_core.hooks import STEP_KINDS, routine_steps
        if registry is not None:
            self._registry = registry
        missing = []
        for kind, ident, *value in routine_steps(args):
            if kind in STEP_KINDS:
                missing += _generic_missing(kind, ident, registry)
                if kind == "compute_set" and len(ident.get("set") or {}) > 1:
                    # one formula per row: {set: {a: .., b: ..}} becomes two
                    # steps in the same order (it runs the same way)
                    for pid, text in ident["set"].items():
                        self.add_generic(kind, {"set": {pid: text}})
                else:
                    self.add_generic(kind, ident)
                continue
            if kind == "action":
                if self.add_action(ident, value[0] if value else None) is None:
                    missing.append(ident)
                continue
            p = registry.get(ident)
            if p is None or getattr(p, "kind", "") != "settable":
                missing.append(ident)
                continue
            self.add_set(p, float(value[0]), merge=False)
        return missing

    def describe(self) -> str:
        """One line for the summary, in run order:
        "field = 190 mT, then vna_reference, then field = 0 mT"."""
        text, after_action = "", False
        for s in self.steps:
            is_action = not isinstance(s, SetStepRow)
            sep = ", then " if (is_action or after_action) else ", "
            text = (text + sep if text else "") + s.text()
            after_action = is_action
        return text


#: The triggers a THROUGHOUT routine offers: (label, when, edge).
THROUGHOUT_TRIGGERS = (("start of each sweep of", "each_sweep", "start"),
                       ("end of each sweep of", "each_sweep", "end"),
                       ("every N points", "every_n_points", None),
                       # 2026-10-04, for abort_if / skip_if: "before each
                       # point" = before it is measured (its values set),
                       # "after each point" = once its values are in
                       ("before each point", "before_point", None),
                       ("after each point", "after_point", None))

#: The THROUGHOUT moments a hook can be loaded into a section from.
THROUGHOUT_WHENS = ("each_sweep", "every_n_points", "before_point", "after_point")

#: Hook keys a ThroughoutSection can show; a hook with any other key is kept
#: verbatim instead (see ScanBuilder._load_hooks).
THROUGHOUT_KEYS = {"when", "axis", "edge", "every", "n", "on_error", "action", "args"}


class ThroughoutSection(RoutineSection):
    """A routine that runs DURING the scan: every N points, or once per sweep.

    Same body as before/after (an ordered list of steps, each waited for), plus a
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

        # line 3 (after "run an action"): how often, what it costs, what if it fails
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
        when = self._trigger()[0]
        sweep = when == "each_sweep"
        for w in (self.axis_combo, self.every_lbl, self.every_spin, self.every_unit):
            w.setVisible(sweep)
        self.n_spin.setVisible(when == "every_n_points")
        self.n_fill.setVisible(not sweep)
        self.every_unit.setText("sweep  ·" if self.every_spin.value() == 1 else "sweeps  ·")
        # The steps only take room when there are some; up to three show
        # without scrolling (a set step is taller than an action step, so the
        # height is measured, not counted).
        shown = self.steps[:3]
        self.rows_scroll.setVisible(bool(shown))
        self.rows_scroll.setFixedHeight(
            sum(s.sizeHint().height() for s in shown) + 4 * len(shown) + 6 if shown else 0)

    def _relayout(self) -> None:
        # the step list is sized to its steps (_sync_trigger_widgets); again
        # once the event loop has laid the step out (a tag row that has just
        # appeared is in the step's size hint only after that pass)
        if hasattr(self, "trigger_combo"):
            self._sync_trigger_widgets()
            QtCore.QTimer.singleShot(0, self._sync_trigger_widgets)
        self.updateGeometry()

    def showEvent(self, ev):
        super().showEvent(ev)
        QtCore.QTimer.singleShot(0, self._sync_trigger_widgets)

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
        # A before/after-each-point routine loaded from a file WITHOUT an
        # on_error is written back without one (it means stop), so an older
        # definition re-saves unchanged.
        self._write_on_error = on_error is not None or when not in ("before_point",
                                                                    "after_point")
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
        elif when == "every_n_points":
            out["n"] = self.n_spin.value()
        if getattr(self, "_write_on_error", True) or self.carry_on.isChecked():
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
    minimum widths, which grow when a routine gets steps (a set step needs
    ~330 px; see RoutineSection.minimumSizeHint) -- not a fixed pixel count.

    The card sets an explicit small minimum width on purpose. Otherwise Qt
    makes the side-by-side layout's minimum (the SUM of the columns) the
    card's minimum, the window can then never be narrower than that, and the
    card never gets the chance to stack: the standalone builder opened 1955 px
    wide on 2026-09-25, wider than it was asked to be.
    """

    #: Heights (min, max) for the two arrangements: stacked, three routines
    #: need more room than side by side.
    WIDE_HEIGHT = (250, 400)       # 250: a THROUGHOUT routine with one step fits
    STACKED_HEIGHT = (330, 380)    # more squeezes the axis stack; the steps scroll

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

    def arrange(self) -> None:
        """Side by side if the columns fit, else stacked. Called on a resize
        AND whenever a routine's steps change (ScanBuilder._rebuild_summary):
        a new step can make a column wider without the card changing size."""
        wide = self.width() >= self._needed_width()
        want = (QtWidgets.QBoxLayout.LeftToRight if wide
                else QtWidgets.QBoxLayout.TopToBottom)
        if self.box.direction() != want:
            self.box.setDirection(want)
            self._apply_height(wide)

    def resizeEvent(self, event):
        self.arrange()
        super().resizeEvent(event)


class _ThroughoutColumn(QtWidgets.QWidget):
    """The THROUGHOUT column: its routines live in a scroll area, so -- as for
    RoutineSection.minimumSizeHint -- it reports the widest routine itself, or
    the RoutinesCard would think it fits where it does not."""

    def __init__(self, sections_fn, scroll_fn):
        super().__init__()
        self._sections = sections_fn
        self._scroll = scroll_fn

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        widest = max((s.minimumSizeHint().width() for s in self._sections()), default=0)
        sc = self._scroll()
        if widest and sc is not None:
            widest += sc.verticalScrollBar().sizeHint().width() + 2 * sc.frameWidth()
        return QtCore.QSize(max(hint.width(), widest), hint.height())


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
    #: The FILE could not be written; the scan itself carries on. Its own signal,
    #: not `failed`: the builder treats `failed` as "the run is over" (Run back
    #: on, Abort off, worker dropped), and a full disk mid-scan used to hand the
    #: buttons back while the scan kept driving the instruments -- no Abort, and
    #: Run free to start a second engine on the same hardware (2026-09-28).
    save_failed = QtCore.Signal(str)
    log = QtCore.Signal(str)                 # what the routines are doing
    #: The engine PAUSED on a fault (list of Fault(name, message)), or
    #: resumed / was aborted from the pause ([]). Emitted from the scan
    #: thread; Qt queues it to the GUI thread.
    paused = QtCore.Signal(object)
    #: RESONANCE WINDOW readout after every point (dict, see WindowRunner.state)
    window = QtCore.Signal(object)
    #: SCOUT PASS readout (dict, see engine.run's on_scout): after every scout
    #: point, and once its mask is made
    scout = QtCore.Signal(object)
    #: A `pause` routine step asks the operator (message, answer); answer is
    #: None when the question is gone. Emitted from the scan thread, queued to
    #: the GUI thread; answer(True/False) may be called from there.
    ask = QtCore.Signal(str, object)
    #: WHERE the scan is: the engine's where_of() of the point just measured
    #: (grid index, zig-zag applied, and each axis's value). Emitted just
    #: BEFORE `progress`, so the progress handler already has it.
    where = QtCore.Signal(object)

    #: Seconds between live redraws. Building the snapshot costs something, and
    #: a 400-point scan of settling points does not need 60 fps.
    LIVE_EVERY_S = 0.4

    #: Scans shorter than this are only saved at the end: a checkpoint of a
    #: 20-point scan costs more than re-running it.
    CHECKPOINT_ABOVE = 100

    def __init__(self, recipe, registry, save_path=None, attrs=None):
        super().__init__()
        self.recipe, self.registry = recipe, registry
        #: extra file attributes: the run info (sample, operator, ...)
        self.attrs = dict(attrs or {})
        self.save_path = Path(save_path) if save_path else None
        self._abort = False
        #: the operator's Pause button (2026-10-07): the engine holds BETWEEN
        #: points while this is True (engine._hold_for_operator)
        self._pause = False
        self._last_live = 0.0
        self._checkpoint_every = 0          # set once the total is known
        self._next_checkpoint = 0           # the point count that triggers the next one
        #: How the run ended, for a QUEUE deciding what comes next:
        #: "done", "aborted" (go on with the next scan) or "error" (stop).
        self.outcome: str | None = None
        #: "Abort all" (a step with scope all / stop_all, or the pause banner's
        #: Abort all): the queue must not start another scan after this one
        self.stop_all = False
        self.stop_reason = ""
        self.error = ""

    def _write(self, ds, done, total):
        """Write the dataset to `save_path` ATOMICALLY (temp file, then replace).

        A netCDF written in place is unreadable while it is being written, and a
        crash mid-write would take the finished points with it. Writing beside
        it and renaming means the file on disk is always a complete scan.
        """
        if self.save_path is None:
            return
        try:
            # autosave.write_dataset: temp file + rename -- or, for a big
            # camera map whose frames are already IN the file
            # (scan_core/framestore.py), the small variables in place
            autosave.write_dataset(ds, self.save_path)
            self.saved.emit(str(self.save_path), done, total)
        except Exception as exc:            # a full disk must not kill the scan
            self.save_failed.emit(f"could not save to {self.save_path}: {exc}")

    def abort(self):
        self._abort = True

    def pause(self):
        """Hold the scan after the point being measured (read by the scan
        thread between points; a plain bool needs no lock)."""
        self._pause = True

    def resume(self):
        self._pause = False

    def _live(self, done, total, snapshot):
        """Redraw, and checkpoint the file, as the scan goes.

        `snapshot` is a factory: the dataset is only BUILT when it is wanted,
        so the points in between cost nothing. One snapshot serves both the
        plot and the checkpoint when they fall on the same point.
        """
        if not self._checkpoint_every:
            # every 1/10 of a LONG scan; a short one is only saved at the end
            self._checkpoint_every = max(1, total // 10) if total > self.CHECKPOINT_ABOVE else 0
            self._next_checkpoint = self._checkpoint_every
        now = time.monotonic()
        # "PASSED the next tenth", not "landed exactly on a multiple of it": a
        # fly scan reports its progress a row (or part of a row) at a time, and
        # 81-pixel rows almost never land on a multiple of 121 -- a fly scan
        # was hardly ever checkpointed. A stepped scan counts 1, 2, 3 ... and
        # checkpoints at exactly the same points as before.
        due_save = (self._checkpoint_every and done < total
                    and done >= self._next_checkpoint)
        if due_save:
            every = self._checkpoint_every
            self._next_checkpoint = (done // every + 1) * every
        due_draw = done >= total or now - self._last_live >= self.LIVE_EVERY_S
        if not (due_save or due_draw):
            return
        ds = snapshot()
        if due_draw:
            self._last_live = now
            self.partial.emit(ds)
        if due_save:
            self._write(ds, done, total)

    def _progress(self, done, total, eta, where=None):
        # `where` is the engine's keyword (engine.where_of); a fly scan sends
        # it once per row and not in between, so None just means "unchanged"
        if where is not None:
            self.where.emit(where)
        self.progress.emit(done, total, eta)

    def run(self):
        try:
            ds = run(self.recipe, self.registry,
                     on_progress=self._progress,
                     should_abort=lambda: self._abort,
                     on_point=self._live,
                     on_log=self.log.emit,
                     # WHEN the run started, into the file's `created`
                     # attribute. It said "live" until 2026-09-28, so the time
                     # was only in the file NAME and a renamed copy had none.
                     created_iso=datetime.now().isoformat(timespec="seconds"),
                     data_path=self.save_path,
                     # a fault PAUSES the scan and waits for the operator
                     # (Lukas, 2026-09-28) instead of ending it
                     on_fault=lambda faults: self.paused.emit(list(faults)),
                     on_window=lambda st: self.window.emit(dict(st)),
                     on_scout=lambda st: self.scout.emit(dict(st)),
                     attrs=self.attrs,
                     # a `pause` step: the banner asks, the scan waits
                     on_pause=lambda msg, answer: self.ask.emit(msg or "", answer),
                     # the operator's Pause button: held between points
                     should_pause=lambda: self._pause)
            n = int(ds.sizes and np.prod([ds.sizes[d] for d in ds.sizes]) or 0)
            self._write(ds, n, n)          # the finished scan, saved for good
            # Abort pressed BETWEEN points ends the engine normally, with the
            # measured part; it is still an abort.
            self.outcome = "aborted" if (self._abort or ds.attrs.get("stopped_by")) else "done"
            self.stop_all = ds.attrs.get("stopped_scope") == "all"
            self.stop_reason = str(ds.attrs.get("stopped_by", ""))
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
            self.stop_all = bool(getattr(exc, "whole_queue", False))
            self.stop_reason = str(getattr(exc, "reason", "") or "")
            # The points measured before it: save and show them, as an abort
            # between two points does (the engine attaches them; None when
            # nothing had been measured yet).
            ds = getattr(exc, "dataset", None)
            if ds is not None:
                n = int(ds.sizes and np.prod([ds.sizes[d] for d in ds.sizes]) or 0)
                self._write(ds, n, n)
                self.done.emit(ds)
            self.failed.emit(f"aborted ({exc})")
        except ScanFault as exc:
            # Only when nobody could be paused for (it should not happen
            # here: the worker always passes a pause handler). Keep the
            # measured points, as for any stop.
            ds = getattr(exc, "dataset", None)
            if ds is not None:
                n = int(ds.sizes and np.prod([ds.sizes[d] for d in ds.sizes]) or 0)
                self._write(ds, n, n)
                self.done.emit(ds)
            self.outcome, self.error = "error", str(exc)
            self.failed.emit(str(exc))
        except Exception as exc:                 # surface validation/compile errors
            # An error DURING the sweep (a settle that timed out): the engine
            # attaches the points measured before it -- save and show them.
            ds = getattr(exc, "dataset", None)
            if ds is not None:
                n = int(ds.sizes and np.prod([ds.sizes[d] for d in ds.sizes]) or 0)
                self._write(ds, n, n)
                self.done.emit(ds)
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

class WindowCard(QtWidgets.QFrame):
    """RESONANCE WINDOW: sweep a slow detector only near the predicted FMR line.

    Lukas, 2026-09-28: "some devices are terribly slow ... I scan most of the
    time in the dark". Shown only when a ticked detector DECLARES window
    support (its describe has a `window` key): for any other detector the
    choice does not exist, and an always-visible card would only be noise.

    Everything here becomes the recipe's `window` block (scan_core/window.py),
    so it travels in the saved .yaml and inside every .nc the scan writes. The
    bottom line is the live readout while a scan runs: where the model put
    the line, where it was found, the window swept and the Meff in use.
    """

    changed = QtCore.Signal()

    MODELS = (("in-plane", "inplane"), ("out-of-plane", "outofplane"))
    DIPS = (("dip (minimum)", "min"), ("peak (maximum)", "max"))

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.registry = None
        self._candidates: list[str] = []
        self._extra_ids: set = set()          # loaded ids the registry lacks
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10); v.setSpacing(6)
        head = QtWidgets.QHBoxLayout()
        tag = QtWidgets.QLabel("RESONANCE WINDOW  ·  sweep only near the FMR line")
        tag.setObjectName("tag")
        head.addWidget(tag); head.addStretch(1)
        self.enable = QtWidgets.QCheckBox("on")
        self.enable.setToolTip(
            "Sweep the detector only within +- margin of the resonance the model\n"
            "predicts from the field (and angle) at each point. Outside it the\n"
            "trace is FILLED with the baseline of the last full sweep, and the\n"
            "file gets a '<detector>_measured' mask saying which bins were\n"
            "measured. The first point, and every N-th, sweep the full band;\n"
            "a line not found in its window widens it and measures again.")
        self.enable.toggled.connect(self._on_toggle)
        head.addWidget(self.enable)
        v.addLayout(head)

        self.body = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(self.body)
        g.setContentsMargins(0, 0, 0, 0); g.setHorizontalSpacing(8); g.setVerticalSpacing(4)

        def lbl(text, tip=""):
            w = QtWidgets.QLabel(text)
            w.setStyleSheet(f"color:{C['muted']};")
            if tip:
                w.setToolTip(tip)
            return w

        def spin(lo, hi, dec, val, suffix="", width=96):
            s = QtWidgets.QDoubleSpinBox()
            s.setRange(lo, hi); s.setDecimals(dec); s.setValue(val)
            if suffix:
                s.setSuffix(suffix)
            s.setFixedWidth(width)
            s.valueChanged.connect(lambda *_: self.changed.emit())
            return s

        self.det_box = QtWidgets.QComboBox()
        self.field_box = QtWidgets.QComboBox()
        self.field_box.setToolTip("The parameter whose value at each point IS the field:\n"
                                  "an axis or a condition (its setpoint is used), or any\n"
                                  "field readout (read at the point).")
        self.angle_box = QtWidgets.QComboBox()
        self.angle_box.setToolTip("In-plane field angle: a parameter (an axis, a condition\n"
                                  "or a readout), or a fixed number.")
        self.angle_fixed = spin(-360, 360, 1, 0.0, " deg", 90)
        for box in (self.det_box, self.field_box, self.angle_box):
            box.currentIndexChanged.connect(lambda *_: self._sync_enabled())
            box.currentIndexChanged.connect(lambda *_: self.changed.emit())
        g.addWidget(lbl("detector"), 0, 0); g.addWidget(self.det_box, 0, 1)
        g.addWidget(lbl("field"), 0, 2); g.addWidget(self.field_box, 0, 3)
        g.addWidget(lbl("angle"), 0, 4); g.addWidget(self.angle_box, 0, 5)
        g.addWidget(self.angle_fixed, 0, 6)

        self.model_box = QtWidgets.QComboBox()
        for text, key in self.MODELS:
            self.model_box.addItem(text, key)
        self.model_box.setToolTip(
            "in-plane: field in the film plane, Kittel with the in-plane uniaxial\n"
            "anisotropy (equilibrium angle solved); out-of-plane: field along the\n"
            "normal, f = gamma'(B - mu0 Meff), no line below mu0 Meff.")
        self.model_box.currentIndexChanged.connect(lambda *_: self._sync_enabled())
        self.model_box.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.g_spin = spin(0.5, 10, 4, 2.0, "", 80)
        self.meff_spin = spin(-5000, 5000, 1, 1750.0, " mT")
        self.meff_spin.setToolTip("mu0 Meff ASSUMED at the start; with 'track' on the\n"
                                  "scan corrects it from every clean line it measures.")
        self.hk_spin = spin(-1000, 1000, 2, 0.0, " mT")
        self.easy_spin = spin(-360, 360, 1, 0.0, " deg", 90)
        g.addWidget(lbl("model"), 1, 0); g.addWidget(self.model_box, 1, 1)
        g.addWidget(lbl("g"), 1, 2); g.addWidget(self.g_spin, 1, 3)
        g.addWidget(lbl("μ0Meff"), 1, 4); g.addWidget(self.meff_spin, 1, 5)
        g.addWidget(lbl("Hk / easy"), 2, 4)
        hk = QtWidgets.QHBoxLayout(); hk.setSpacing(4)
        hk.addWidget(self.hk_spin); hk.addWidget(self.easy_spin)
        g.addLayout(hk, 2, 5, 1, 2)

        self.margin_spin = spin(1, 1e5, 0, 300.0, " MHz")
        self.margin_spin.setToolTip("Half width of the window around the predicted line.\n"
                                    "Make it several linewidths: outside it the line's\n"
                                    "tail is replaced by the baseline.")
        self.dip_box = QtWidgets.QComboBox()
        for text, key in self.DIPS:
            self.dip_box.addItem(text, key)
        self.dip_box.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.track_box = QtWidgets.QCheckBox("track Meff")
        self.track_box.setChecked(True)
        self.track_box.toggled.connect(lambda *_: self.changed.emit())
        self.full_spin = QtWidgets.QSpinBox()
        self.full_spin.setRange(0, 100000); self.full_spin.setValue(20)
        self.full_spin.setFixedWidth(80)
        self.full_spin.setToolTip("A full-band sweep every N points (and always at the\n"
                                  "first) refreshes the baseline. 0 = only the first.")
        self.full_spin.valueChanged.connect(lambda *_: self.changed.emit())
        g.addWidget(lbl("margin ±"), 2, 0); g.addWidget(self.margin_spin, 2, 1)
        g.addWidget(lbl("line is a"), 2, 2); g.addWidget(self.dip_box, 2, 3)
        g.addWidget(self.track_box, 3, 1)
        g.addWidget(lbl("full sweep every"), 3, 2); g.addWidget(self.full_spin, 3, 3)
        g.setColumnStretch(7, 1)
        v.addWidget(self.body)

        self.live = QtWidgets.QLabel("")
        self.live.setStyleSheet(f"color:{C['accent']}; font-size:11px;")
        self.live.setWordWrap(True)
        v.addWidget(self.live)
        self._sync_enabled()
        self.hide()

    # ---- contents -----------------------------------------------------------
    def set_registry(self, registry) -> None:
        """Refill the field/angle choices from `registry` (units decide)."""
        from scan_core.window import ANGLE_UNITS, FIELD_UNITS
        self.registry = registry
        params = list(registry.settables()) + list(registry.gettables())

        def fill(box, units, first=None):
            keep = box.currentData()
            box.blockSignals(True)
            box.clear()
            if first:
                box.addItem(*first)
            for p in params:
                if (p.unit or "").strip().lower() in units and (p.unit or "").strip():
                    box.addItem(f"{p.label}  ·  {p.id}", p.id)
            i = box.findData(keep)
            box.setCurrentIndex(i if i >= 0 else 0)
            box.blockSignals(False)

        fill(self.field_box, FIELD_UNITS)
        fill(self.angle_box, ANGLE_UNITS, first=("fixed:", ""))
        self._extra_ids = set()
        self._sync_enabled()

    def set_detectors(self, ids: list[str]) -> None:
        """The ticked detectors that support a window. The card shows itself
        only when there is one (or a loaded window is switched on)."""
        self._candidates = list(ids)
        keep = self.det_box.currentData()
        self.det_box.blockSignals(True)
        self.det_box.clear()
        for pid in ids:
            p = self.registry.get(pid) if self.registry is not None else None
            self.det_box.addItem(f"{getattr(p, 'label', pid)}  ·  {pid}", pid)
        if keep and keep not in ids and self.enable.isChecked():
            # a loaded detector that is no longer ticked: keep it visible, so
            # the recipe (and its validation error) say what is wrong
            self.det_box.addItem(f"(not ticked)  ·  {keep}", keep)
        i = self.det_box.findData(keep)
        self.det_box.setCurrentIndex(i if i >= 0 else 0)
        self.det_box.blockSignals(False)
        self.setVisible(bool(ids) or self.enable.isChecked())

    def _on_toggle(self, *_):
        self._sync_enabled()
        self.changed.emit()

    def _sync_enabled(self):
        on = self.enable.isChecked()
        self.body.setEnabled(on)
        self.angle_fixed.setEnabled(on and not self.angle_box.currentData())
        inplane = self.model_box.currentData() == "inplane"
        for w in (self.hk_spin, self.easy_spin):
            w.setEnabled(on and inplane)
        if not on:
            self.live.setText("")

    # ---- recipe round trip ------------------------------------------------------
    def to_block(self) -> dict | None:
        if not self.enable.isChecked() or self.det_box.currentData() is None:
            return None
        angle = self.angle_box.currentData()
        return {
            "detector": self.det_box.currentData(),
            "field": self.field_box.currentData(),
            "angle": angle if angle else float(self.angle_fixed.value()),
            "model": self.model_box.currentData(),
            "params": {"g": float(self.g_spin.value()),
                       "meff_mT": float(self.meff_spin.value()),
                       "hk_mT": float(self.hk_spin.value()),
                       "easy_axis_deg": float(self.easy_spin.value())},
            "margin_MHz": float(self.margin_spin.value()),
            "dip": self.dip_box.currentData(),
            "track": bool(self.track_box.isChecked()),
            "full_every": int(self.full_spin.value()),
        }

    def load_block(self, block) -> list[str]:
        """Fill the card from a recipe's `window` block (None = switch it off).
        Returns the ids this registry does not have."""
        from scan_core.window import normalize
        missing = []
        widgets = (self.enable, self.det_box, self.field_box, self.angle_box,
                   self.model_box, self.dip_box, self.track_box, self.full_spin,
                   self.g_spin, self.meff_spin, self.hk_spin, self.easy_spin,
                   self.margin_spin, self.angle_fixed)
        for w in widgets:
            w.blockSignals(True)
        try:
            if not block:
                self.enable.setChecked(False)
                return []
            w = normalize(block)

            def pick(box, pid):
                if pid is None:
                    return
                i = box.findData(pid)
                if i < 0:
                    if self.registry is None or self.registry.get(pid) is None:
                        missing.append(pid)
                    box.addItem(f"(missing)  ·  {pid}", pid)
                    i = box.findData(pid)
                box.setCurrentIndex(i)

            pick(self.det_box, w.get("detector"))
            pick(self.field_box, w.get("field"))
            ang = w.get("angle")
            if isinstance(ang, str):
                pick(self.angle_box, ang)
            else:
                self.angle_box.setCurrentIndex(max(0, self.angle_box.findData("")))
                self.angle_fixed.setValue(float(ang or 0.0))
            self.model_box.setCurrentIndex(max(0, self.model_box.findData(w["model"])))
            self.dip_box.setCurrentIndex(max(0, self.dip_box.findData(w["dip"])))
            p = w["params"]
            self.g_spin.setValue(p["g"]); self.meff_spin.setValue(p["meff_mT"])
            self.hk_spin.setValue(p["hk_mT"]); self.easy_spin.setValue(p["easy_axis_deg"])
            self.margin_spin.setValue(float(w["margin_MHz"]))
            self.track_box.setChecked(bool(w["track"]))
            self.full_spin.setValue(int(w["full_every"]))
            self.enable.setChecked(True)
        finally:
            for x in widgets:
                x.blockSignals(False)
            self._sync_enabled()
            self.setVisible(bool(self._candidates) or self.enable.isChecked())
        return missing

    def describe(self) -> str:
        """One clause for the scan summary."""
        b = self.to_block()
        if not b:
            return ""
        model = dict((k, t) for t, k in self.MODELS).get(b["model"], b["model"])
        return (f"window ±{b['margin_MHz']:g} MHz on {b['detector']} ({model}, "
                f"μ0Meff {b['params']['meff_mT']:g} mT"
                + (", tracked" if b["track"] else "") + ")")

    # ---- live readout -----------------------------------------------------------
    def show_state(self, st: dict) -> None:
        """What the last KEPT point did (engine on_window, via ScanWorker)."""
        def ghz(x):
            return "--" if x is None or not math.isfinite(x) else f"{x / 1e9:.4f}"
        if not st or "window_lo_Hz" not in st:
            self.live.setText("")
            return
        kind = "FULL sweep" + (f" ({st.get('reason')})" if st.get("reason") else "") \
            if st.get("full_sweep") else "window"
        self.live.setText(
            f"point {st['points']}: {kind} {ghz(st['window_lo_Hz'])}-{ghz(st['window_hi_Hz'])} GHz"
            f"   ·   f_res predicted {ghz(st['fres_pred_Hz'])} GHz, found {ghz(st['fres_fit_Hz'])} GHz"
            f"   ·   μ0Meff {st['meff_mT']:.1f} mT"
            f"   ·   {100 * st.get('fraction_measured', float('nan')):.0f} % of the bins measured")


class ScoutSettingChip(QtWidgets.QFrame):
    """One scout-only setting, `RF power [12.000 dBm] x`, compact enough to
    sit in a line with the others (the open scout section is short; a full
    condition row per setting would push its options out of sight). The
    value box is a condition row's: the parameter's live limits, whole numbers
    for an int parameter."""

    changed = QtCore.Signal()
    remove = QtCore.Signal(object)

    def __init__(self, param, value=None):
        super().__init__()
        self.param = param
        self.setObjectName("axis")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 1, 2, 1); lay.setSpacing(4)
        name = QtWidgets.QLabel(param.label)
        name.setToolTip(f"{param.id}: held at this value during the scout only,\n"
                        f"put back for the real scan (also after an Abort)")
        lay.addWidget(name)
        self.integer = bool(getattr(param, "integer", False))
        lo, hi = FixedRow._finite_limits(self)
        self.value_box = _value_box(param)
        self.value_box.setRange(lo, hi)
        self.value_box.setDecimals(0 if self.integer else 3)
        self.value_box.setFixedWidth(104)
        if param.unit:
            self.value_box.setSuffix(f" {param.unit}")
        self.value_box.setValue(float(value) if value is not None
                                else FixedRow._default_value(lo, hi))
        self.value_box.valueChanged.connect(lambda *_: self.changed.emit())
        lay.addWidget(self.value_box)
        rm = QtWidgets.QPushButton("✕"); rm.setObjectName("danger"); rm.setFixedWidth(22)
        rm.setStyleSheet("padding: 0px;")
        rm.clicked.connect(lambda: self.remove.emit(self))
        lay.addWidget(rm)

    def value(self) -> float:
        v = self.value_box.value()
        return int(round(v)) if self.integer else v


class ScoutSection(QtWidgets.QFrame):
    """SCOUT PASS: take a quick look first, then measure in detail only where
    something is happening (scan_core/scout.py).

    Lukas, 2026-10-08, on the XY MASK card it replaces: it "awkwardly jumps in
    place when you get two axes" -- a card that appeared with the second axis
    and squeezed everything around it. So this is a SECTION that is always
    there, at the bottom of the axis stack, collapsed to one line ("SCOUT PASS
    · off -- tick 'scout' on the axes to scout") and opening in place. Its
    height never depends on the number of axes; opening it takes the room
    from the axis list above (a scroll area), not from the cards below.

    Which axes are scouted is ticked on the axis rows themselves (with their
    coarse step); everything else -- what the scout looks at, how it decides,
    the margin, what the outer axes do, the scout-only settings, a mask file --
    is here. It all becomes the recipe's `scout` block, so it travels in the
    .yaml and in every .nc.

    The GRID PREVIEW draws which points the scout will visit on the scan's
    grid (first rig test, 2026-10-08: a coarse row step of 3 fine rows looked
    like 1 on the camera, "it scans x, jumps one down"). For a mask FILE,
    "Preview mask" draws the mask it gives over the scan's own grid.
    """

    changed = QtCore.Signal()
    #: opened (True) or closed (False): the builder moves the room for the
    #: body between the axis list and the section, so the card keeps its height
    expanded_changed = QtCore.Signal(bool)

    SOURCES = (("the scout pass (measure)", "measure"),
               ("an image or a matrix", "picture"),
               ("an earlier scan (.nc)", "scan"))
    KEEPS = (("ABOVE a threshold", "above"),
             ("BELOW a threshold", "below"),
             ("DEVIATES from the background", "deviates"))
    THRESHOLDS = (("auto (Otsu)", "auto"), ("value", "value"),
                  ("fraction of the range", "fraction"))
    PICTURES = ("Image or matrix (*.png *.tif *.tiff *.bmp *.jpg *.jpeg *.csv *.txt "
                "*.dat *.npy);;All files (*)")
    SCANS = "Scan (*.nc);;All files (*)"
    #: the body's height when open: it takes the room from the axis list
    #: above it, never from the cards below
    BODY_MAX = 104
    #: the side of the grid / mask preview, in pixels
    PREVIEW_PX = 100

    def __init__(self, recipe_fn=None):
        super().__init__()
        self.setObjectName("scoutSection")
        self.registry = None
        #: builds the current recipe (the builder's); the previews need the grid
        self._recipe_fn = recipe_fn
        self._axes: dict = {}               # the ticked axes {name: step}
        self._margins: dict = {}            # their margins {name: auto | points}
        self._outer: list[str] = []
        self._fly = False
        self._expanded = False
        self.setStyleSheet(
            f"QFrame#scoutSection {{ border-top: 1px solid {C['border']}; }}")
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 6, 0, 0); v.setSpacing(4)

        # the ONE line that is always there
        head = QtWidgets.QHBoxLayout(); head.setSpacing(8)
        self.toggle = QtWidgets.QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(QtCore.Qt.RightArrow)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle.setText("SCOUT PASS")
        self.toggle.setStyleSheet(
            f"QToolButton {{ border: none; color: {C['accent']}; font-weight: 800;"
            f" letter-spacing: 1px; }}")
        self.toggle.setToolTip(
            "Scout pass: take a quick look first, then measure in detail only\n"
            "where something is happening. Click to show the options.")
        self.toggle.toggled.connect(self.set_expanded)
        head.addWidget(self.toggle)
        self.state = QtWidgets.QLabel()
        self.state.setStyleSheet(f"color:{C['muted']};")
        # cut, never widen the window (the routines card's rule)
        self.state.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                 QtWidgets.QSizePolicy.Preferred)
        head.addWidget(self.state, 1)
        v.addLayout(head)

        # The body is WIDE and SHORT on purpose: in the suite the middle
        # column is ~1100 px wide but the axis stack card only ~260 px tall,
        # so the options sit in three compact rows with the preview beside
        # them, and the body is never taller than BODY_MAX.
        self.body = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(self.body)
        outer.setContentsMargins(4, 0, 4, 2); outer.setSpacing(12)
        left = QtWidgets.QVBoxLayout(); left.setSpacing(4)
        outer.addLayout(left, 1)

        def lbl(text, tip=""):
            w = QtWidgets.QLabel(text)
            w.setStyleSheet(f"color:{C['muted']};")
            if tip:
                w.setToolTip(tip)
            return w

        def dspin(lo, hi, dec, val, width=90):
            s = QtWidgets.QDoubleSpinBox()
            s.setRange(lo, hi); s.setDecimals(dec); s.setValue(val)
            s.setFixedWidth(width)
            s.valueChanged.connect(lambda *_: self._edited())
            return s

        def combo(items):
            b = QtWidgets.QComboBox()
            for text, key in items:
                b.addItem(text, key)
            b.currentIndexChanged.connect(lambda *_: self._edited())
            return b

        def row():
            h = QtWidgets.QHBoxLayout(); h.setSpacing(6)
            left.addLayout(h)
            return h

        # row 1: where the mask comes from, and what the scout looks at (or
        # the file)
        self.source_box = combo(self.SOURCES)
        self.source_box.setToolTip(
            "Where the mask comes from: the scout pass measures it (every k-th\n"
            "point of the ticked axes); or a picture you made (exactly two\n"
            "scouted axes); or an earlier scan of the same axes (.nc).")
        self.det_box = QtWidgets.QComboBox()
        self.det_box.setToolTip("What the scout reads: any detector that gives ONE\n"
                                "number per point (a reflectivity, a power, a lock-in R).")
        self.det_box.setMinimumWidth(160)
        self.det_box.currentIndexChanged.connect(lambda *_: self._edited())
        self.det_label = lbl("look at")
        self.file_edit = QtWidgets.QLineEdit()
        self.file_edit.editingFinished.connect(self._edited)
        self.file_edit.setToolTip(
            "An IMAGE: bright = measure (with 'above'); grayscale or colour (read\n"
            "as brightness). A MATRIX: one row per line. Columns run along the\n"
            "FIRST scouted axis (X), rows along the second (Y).\n"
            "An earlier SCAN: its axes are found by name (or by the parameter\n"
            "they swept); points outside its range are measured.")
        self.browse = QtWidgets.QPushButton("Browse…")
        self.browse.clicked.connect(self._browse)
        self.var_edit = QtWidgets.QLineEdit()
        self.var_edit.setPlaceholderText("variable")
        self.var_edit.setFixedWidth(110)
        self.var_edit.setToolTip("The variable read from the earlier scan (e.g. lockin_r).")
        self.var_edit.editingFinished.connect(self._edited)
        self.file_label = lbl("file")
        r1 = row()
        r1.addWidget(lbl("mask from")); r1.addWidget(self.source_box)
        r1.addWidget(self.det_label); r1.addWidget(self.det_box, 1)
        r1.addWidget(self.file_label); r1.addWidget(self.file_edit, 1)
        r1.addWidget(self.var_edit); r1.addWidget(self.browse)

        # row 2: how to decide, and the margin
        self.keep_box = combo(self.KEEPS)
        self.keep_box.setToolTip(
            "above / below: a threshold (Otsu's auto splits two groups -- substrate\n"
            "and elements).\n"
            "deviates: |reading - median| > k x noise, noise = 1.4826 x the median\n"
            "absolute deviation. Finds peaks AND dips, on a sample with several\n"
            "levels or a gradient too. Assumes the background is most of the scout.")
        self.thr_box = combo(self.THRESHOLDS)
        self.thr_box.setToolTip("auto: Otsu's method, the level that best splits the\n"
                                "readings into two groups.\n"
                                "fraction: 0 = the lowest reading, 1 = the highest.")
        self.thr_spin = dspin(-1e12, 1e12, 6, 0.5, 100)
        self.k_label = lbl("k", "How many noise widths away from the background a\n"
                                "reading must be to be measured (default 4).")
        self.k_spin = dspin(0.1, 100, 1, 4.0, 64)
        self.thr_label = lbl("threshold")
        # The MARGIN is per axis since 2026-10-09 (Advanced > SCOUT on each
        # row): one box here could only show the largest of several. This
        # label says where it went.
        self.margin_lbl = lbl("margin: per axis, in Advanced",
                              "How far the mask is grown, in grid points, is set\n"
                              "on each scouted axis: Advanced > SCOUT > margin\n"
                              "(auto = half that axis's scout step).")
        r2 = row()
        r2.addWidget(lbl("measure where it is")); r2.addWidget(self.keep_box)
        r2.addWidget(self.thr_label); r2.addWidget(self.thr_box); r2.addWidget(self.thr_spin)
        r2.addWidget(self.k_label); r2.addWidget(self.k_spin)
        r2.addSpacing(8)
        r2.addWidget(self.margin_lbl)
        r2.addStretch(1)

        # row 3: the outer axes, and settings held during the scout only
        self.outer_box = combo((("once, at their first values", "once"),
                                ("again at every step", "each")))
        self.outer_box.setToolTip(
            "The axes OUTSIDE (above) the scouted ones:\n"
            "once -- one scout at their first values, one mask for all of them\n"
            "  (a patterned sample does not move with the field);\n"
            "each -- a new scout at every step of them, for a feature that moves\n"
            "  with them (an FMR line drifting with the angle).\n"
            "Axes INSIDE a scouted one are held at their first value during the\n"
            "scout, and the mask holds for all their values.")
        self.outer_label = lbl("outer axes")
        self.add_setting = QtWidgets.QComboBox()
        self.add_setting.setToolTip(
            "A setting held ONLY during the scout and put back for the real scan\n"
            "-- also after an Abort or an error: a short lock-in time constant,\n"
            "one scope average, a higher power. The scout is then fast even with\n"
            "the same instrument.")
        self.add_setting.setMaximumWidth(190)
        self.add_setting.activated.connect(self._add_setting_from_combo)
        self.settings_box = QtWidgets.QHBoxLayout(); self.settings_box.setSpacing(4)
        self.setting_rows: list = []
        r3 = row()
        r3.addWidget(self.outer_label); r3.addWidget(self.outer_box)
        r3.addSpacing(8)
        r3.addWidget(self.add_setting)
        r3.addLayout(self.settings_box)
        r3.addStretch(1)

        # row 4 (a picture only): where it lies
        self.extent_box = QtWidgets.QCheckBox("place the picture")
        self.extent_box.setToolTip(
            "Off: the picture covers exactly the scan's area, its first row at the\n"
            "START of the Y axis. On: X of the first / last column and Y of the\n"
            "first / last row (row 0 = the top of an image), in the axes' unit.\n"
            "Scan points outside the picture are measured.")
        self.extent_box.toggled.connect(lambda *_: self._edited())
        self.ext = [dspin(-1e9, 1e9, 3, val, 80) for val in (-50.0, 50.0, -50.0, 50.0)]
        r4 = row()
        r4.addWidget(self.extent_box)
        self._ext_labels = []
        for text, w in zip(("x", "", "y", ""), self.ext):
            if text:
                t = lbl(text)
                self._ext_labels.append(t)
                r4.addWidget(t)
            r4.addWidget(w)
        r4.addStretch(1)
        left.addStretch(1)

        # the preview, beside the options: which points the scout looks at
        # (or the mask a file gives), and the numbers that go with it
        self.picture = QtWidgets.QLabel()
        self.picture.setFixedSize(self.PREVIEW_PX, self.PREVIEW_PX)
        self.picture.setAlignment(QtCore.Qt.AlignCenter)
        self.picture.setStyleSheet(f"background:{C['code_bg']}; border-radius:4px;")
        outer.addWidget(self.picture, 0, QtCore.Qt.AlignTop)
        side = QtWidgets.QVBoxLayout(); side.setSpacing(4)
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color:{C['accent']}; font-size:11px;")
        self.info.setFixedWidth(230)
        self.info.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        side.addWidget(self.info)
        self.preview_btn = QtWidgets.QPushButton("Preview mask")
        self.preview_btn.setToolTip("Draw the mask this FILE gives on the scan's grid\n"
                                    "(grey = the source, amber = measured).")
        self.preview_btn.clicked.connect(self.preview_mask)
        side.addWidget(self.preview_btn, 0, QtCore.Qt.AlignLeft)
        side.addStretch(1)
        outer.addLayout(side, 0)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setWidget(self.body)
        # opened, the body gets its full height: it is the axis list above it
        # that gives up the room (scrolling if it must), never the cards below
        self.scroll.setFixedHeight(self.BODY_MAX)
        v.addWidget(self.scroll)
        self.scroll.setVisible(False)
        self._sync()

    # ---- open / closed ---------------------------------------------------------
    def set_expanded(self, on: bool) -> None:
        self._expanded = bool(on)
        self.toggle.blockSignals(True)
        self.toggle.setChecked(self._expanded)
        self.toggle.blockSignals(False)
        self.toggle.setArrowType(QtCore.Qt.DownArrow if on else QtCore.Qt.RightArrow)
        self.scroll.setVisible(self._expanded)
        self.expanded_changed.emit(self._expanded)
        if on:
            self.preview_grid()

    def expanded(self) -> bool:
        return self._expanded

    # ---- contents -------------------------------------------------------------
    def set_registry(self, registry) -> None:
        """The detectors the scout can use (readable, one number per point),
        and the settables a scout-only setting can hold."""
        self.registry = registry
        keep = self.det_box.currentData()
        self.det_box.blockSignals(True)
        self.det_box.clear()
        for p in registry.gettables():
            if getattr(p, "axes", None) or getattr(p, "dtype", "float") not in ("float", "int"):
                continue
            self.det_box.addItem(f"{p.label}  ·  {p.id}", p.id)
        i = self.det_box.findData(keep)
        if keep and i < 0:
            self.det_box.addItem(f"(missing)  ·  {keep}", keep)
            i = self.det_box.findData(keep)
        self.det_box.setCurrentIndex(max(0, i))
        self.det_box.blockSignals(False)
        self.add_setting.blockSignals(True)
        self.add_setting.clear()
        self.add_setting.addItem("＋ scout-only setting ...", None)
        for p in registry.settables():
            self.add_setting.addItem(f"{p.label}  ·  {p.id}", p.id)
        self.add_setting.blockSignals(False)
        for row in list(self.setting_rows):
            self._remove_setting(row)

    def set_axes(self, axes: dict, outer: list[str], fly: bool,
                 margins: dict | None = None) -> None:
        """The ticked axes ({name: step}, X first), the unscouted axes outside
        them, whether the stack has a fly axis, and each ticked axis's margin
        ({name: "auto" | grid points}; missing = auto) -- set on the rows."""
        self._axes, self._outer, self._fly = dict(axes), list(outer), fly
        self._margins = {n: (margins or {}).get(n, "auto") for n in self._axes}
        self._sync()
        if self._expanded:
            self.preview_grid()

    # ---- settings held during the scout ------------------------------------------
    def _add_setting_from_combo(self, i):
        pid = self.add_setting.itemData(i)
        self.add_setting.setCurrentIndex(0)
        if pid:
            self.add_scout_setting(pid)

    def add_scout_setting(self, pid, value=None):
        """One row `parameter = value`, held during the scout only."""
        p = self.registry.get(pid) if self.registry is not None else None
        if p is None:
            return None
        for row in self.setting_rows:
            if row.param.id == pid:
                if value is not None:
                    row.value_box.setValue(float(value))
                return row
        if value is None:
            try:
                value = float(p.get())     # start from what it is now
            except Exception:
                value = None
        row = ScoutSettingChip(p, value)
        row.changed.connect(self._edited)
        row.remove.connect(self._remove_setting)
        self.setting_rows.append(row)
        self.settings_box.addWidget(row)
        self._edited()
        return row

    def _remove_setting(self, row):
        if row in self.setting_rows:
            self.setting_rows.remove(row)
        self.settings_box.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        self._edited()

    # ---- the visible state ---------------------------------------------------------
    def _browse(self):
        picture = self.source_box.currentData() == "picture"
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Mask source", self.file_edit.text(),
            self.PICTURES if picture else self.SCANS)
        if fn:
            self.file_edit.setText(fn)
            self._edited()

    def _edited(self, *_):
        self._sync()
        self.changed.emit()

    def _sync(self):
        src = self.source_box.currentData()
        measure, picture = src == "measure", src == "picture"
        for w in (self.file_label, self.file_edit, self.browse):
            w.setVisible(not measure)
        self.var_edit.setVisible(src == "scan")
        self.file_edit.setPlaceholderText(
            "an image (.png .tif ...) or a matrix (.csv .txt .npy)" if picture
            else "an earlier scan of the same axes (.nc)")
        self.det_box.setVisible(measure)
        self.det_label.setVisible(measure)
        dev = self.keep_box.currentData() == "deviates"
        for w in (self.thr_label, self.thr_box, self.thr_spin):
            w.setVisible(not dev)
        self.k_label.setVisible(dev); self.k_spin.setVisible(dev)
        self.thr_spin.setEnabled(self.thr_box.currentData() != "auto")
        self.outer_box.setEnabled(bool(self._outer) and measure)
        self.outer_label.setToolTip(
            ", ".join(self._outer) if self._outer else "no unscouted axis outside the scouted ones")
        self.extent_box.setVisible(picture)
        for w in self.ext:
            w.setVisible(picture)
            w.setEnabled(self.extent_box.isChecked())
        for t in self._ext_labels:
            t.setVisible(picture)
        # the scout-only settings belong to a MEASURED scout
        self.add_setting.setEnabled(measure)
        self.preview_btn.setVisible(not measure)
        self.preview_btn.setEnabled(bool(self.file_edit.text().strip()) and bool(self._axes))
        self.state.setText(self.state_text())

    def state_text(self) -> str:
        """The one line shown next to SCOUT PASS, open or closed."""
        if not self._axes:
            return "off  —  tick 'scout' on the axes to scout"
        def margin(n):
            m = self._margins.get(n, "auto")
            return "" if m == "auto" else f" (margin {float(m):g})"
        axes = ", ".join(f"{n} every {s}{margin(n)}" for n, s in self._axes.items())
        src = self.source_box.currentData()
        if src == "measure":
            det = self.det_box.currentData() or "?"
            what = f"look at {det}"
        else:
            what = f"mask from {Path(self.file_edit.text().strip() or '?').name}"
        how = self.keep_box.currentData()
        if how == "deviates":
            how = f"where it deviates (k {self.k_spin.value():g})"
        else:
            t = self.thr_box.currentData()
            level = ("auto" if t == "auto" else f"{self.thr_spin.value():g}" if t == "value"
                     else f"{self.thr_spin.value():g} of the range")
            how = f"where it is {how} the threshold ({level})"
        extra = ""
        if self._outer and src == "measure":
            extra = ("  ·  again at every " + ", ".join(self._outer)
                     if self.outer_box.currentData() == "each" else "")
        if self.setting_rows:
            extra += f"  ·  {len(self.setting_rows)} scout-only setting(s)"
        return f"on: {axes}  ·  {what}, measure {how}{extra}"

    # ---- recipe round trip --------------------------------------------------------
    def to_block(self) -> dict | None:
        if not self._axes:
            return None
        b: dict = {"axes": dict(self._axes)}
        src = self.source_box.currentData()
        path = self.file_edit.text().strip()
        if src == "measure":
            if self.det_box.currentData():
                b["detector"] = self.det_box.currentData()
        elif src == "picture":
            b["from"] = path
        else:
            b["from"] = {"file": path, "detector": self.var_edit.text().strip()}
        keep = self.keep_box.currentData()
        b["keep"] = keep
        if keep == "deviates":
            b["k"] = float(self.k_spin.value())
        else:
            t = self.thr_box.currentData()
            b["threshold"] = ("auto" if t == "auto" else float(self.thr_spin.value())
                              if t == "value" else {"fraction": float(self.thr_spin.value())})
        b["margin"] = self.margin_value()
        if src == "measure" and self._outer and self.outer_box.currentData() == "each":
            b["per_outer"] = "each"
        if self.setting_rows and src == "measure":
            b["settings"] = {r.param.id: r.value() for r in self.setting_rows}
        if src == "picture" and self.extent_box.isChecked():
            x0, x1, y0, y1 = (float(w.value()) for w in self.ext)
            b["extent"] = {"x": [x0, x1], "y": [y0, y1]}
        return b

    def margin_value(self):
        """The block's `margin` from the per-axis margins: `auto` when every
        axis is auto, ONE number when they all agree (the form every recipe
        before 2026-10-09 has, so such a definition saves back unchanged),
        otherwise {axis: auto | points} for every scouted axis."""
        vals = [self._margins.get(n, "auto") for n in self._axes]
        if all(v == "auto" for v in vals):
            return "auto"
        if len({str(v) for v in vals}) == 1:
            return float(vals[0])
        return {n: (v if v == "auto" else float(v))
                for n, v in zip(self._axes, vals)}

    def load_block(self, block) -> list[str]:
        """Fill the section from a recipe's `scout` block (None = off; the
        ticks AND the per-axis margins are the builder's, on the rows).
        Returns the ids this registry does not have."""
        from scan_core.scout import is_picture, spec_of
        widgets = (self.source_box, self.det_box, self.file_edit, self.var_edit,
                   self.keep_box, self.thr_box, self.thr_spin, self.k_spin,
                   self.outer_box,
                   self.extent_box, *self.ext)
        missing: list[str] = []
        for row in list(self.setting_rows):
            self._remove_setting(row)
        for w in widgets:
            w.blockSignals(True)
        try:
            if not block:
                return []
            s = spec_of(block)
            src = ("measure" if not s["from"] else
                   "picture" if is_picture(s["from_file"]) else "scan")
            self.source_box.setCurrentIndex(self.source_box.findData(src))
            self.file_edit.setText(s["from_file"] or "")
            self.var_edit.setText(str(s["from_detector"] or "") if src == "scan" else "")
            det = s["detector"]
            if det and src == "measure":
                i = self.det_box.findData(det)
                if i < 0:
                    missing.append(det)
                    self.det_box.addItem(f"(missing)  ·  {det}", det)
                    i = self.det_box.findData(det)
                self.det_box.setCurrentIndex(i)
            self.keep_box.setCurrentIndex(max(0, self.keep_box.findData(s["keep"])))
            try:
                self.k_spin.setValue(float(s["k"]))
            except (TypeError, ValueError):
                pass
            t = s["threshold"]
            if t == "auto":
                self.thr_box.setCurrentIndex(0)
            elif isinstance(t, dict):
                self.thr_box.setCurrentIndex(2); self.thr_spin.setValue(float(t["fraction"]))
            else:
                self.thr_box.setCurrentIndex(1); self.thr_spin.setValue(float(t))
            self.outer_box.setCurrentIndex(1 if s["per_outer"] == "each" else 0)
            e = s["extent"]
            self.extent_box.setChecked(bool(e))
            if e:
                vals = list(e.get("x") or (-50, 50)) + list(e.get("y") or (-50, 50))
                for w, val in zip(self.ext, vals):
                    w.setValue(float(val))
        finally:
            for w in widgets:
                w.blockSignals(False)
        for pid, value in (s["settings"] or {}).items():
            if self.add_scout_setting(pid, value) is None:
                missing.append(pid)
        self._sync()
        return missing

    def describe(self) -> str:
        """One clause for the scan summary."""
        b = self.to_block()
        if not b:
            return ""
        return "scout pass " + self.state_text()[len("on: "):]

    # ---- previews -------------------------------------------------------------------
    def preview_grid(self) -> None:
        """Which points the scout visits, on the scan's grid: every point a
        dim dot, the scout's points amber. The first two scouted axes as a map
        (the outer one up the page), one axis as a strip. With the list of
        indices per axis -- the coarse grid always ends on the LAST point, so
        the last gap may be shorter."""
        from scan_core import scout as S
        if not self._axes or self.source_box.currentData() != "measure":
            if self.source_box.currentData() == "measure":
                self.picture.clear()
                self.info.setText("Tick 'scout' on one or more axes; this shows the "
                                  "points the scout will look at.")
            return
        try:
            est = S.estimate(self._recipe_fn())
        except Exception:
            est = None
        if not est:
            self.picture.clear()
            self.info.setText("")
            return
        names, coarse, sizes = est["names"], est["coarse"], est["sizes"]
        # the map: rows = the outer of the first two scouted axes, columns = the inner
        if len(names) >= 2:
            ny, nx = sizes[0], sizes[1]
            rows, cols = set(coarse[names[0]]), set(coarse[names[1]])
        else:
            ny, nx = 1, sizes[0]
            rows, cols = {0}, set(coarse[names[0]])
        cell = int(max(2, min(10, (self.PREVIEW_PX - 4) // max(nx, ny, 1))))
        img = QtGui.QImage(nx * cell, ny * cell, QtGui.QImage.Format_RGB32)
        img.fill(QtGui.QColor(C["code_bg"]))
        p = QtGui.QPainter(img)
        # every grid point a faint dot, the scout's points a full amber cell:
        # on a big grid the dots merge into a grey ground and the scout's
        # lattice still stands out
        dim = QtGui.QColor(C["muted"]); dim.setAlpha(150)
        hit = QtGui.QColor(C["accent"])
        dot = max(1, cell // 3)
        for i in range(ny):
            for j in range(nx):
                if i in rows and j in cols:
                    p.fillRect(j * cell, i * cell, max(1, cell - (cell > 2)),
                               max(1, cell - (cell > 2)), hit)
                else:
                    p.fillRect(j * cell + (cell - dot) // 2, i * cell + (cell - dot) // 2,
                               dot, dot, dim)
        p.end()
        pix = QtGui.QPixmap.fromImage(img)
        side = self.PREVIEW_PX - 4
        if pix.width() > side or pix.height() > side:
            pix = pix.scaled(side, side, QtCore.Qt.KeepAspectRatio,
                             QtCore.Qt.SmoothTransformation)
        self.picture.setPixmap(pix)

        def idx_text(c):
            c = list(c)
            return (", ".join(map(str, c)) if len(c) <= 7 else
                    ", ".join(map(str, c[:4])) + ", ..., " + ", ".join(map(str, c[-2:])))
        lines = [f"{n}: {idx_text(coarse[n])}  ({len(coarse[n])} of {sz})"
                 for n, sz in zip(names, sizes)]
        n_pts, blocks = est["points"], est["blocks"]
        lines.append(f"scout: {n_pts:,} points" + (f" x {blocks} (again at every "
                                                    f"{', '.join(est['outer'])})"
                                                    if blocks > 1 else ""))
        if est["inner"]:
            lines.append(f"held at their first value during the scout: "
                         f"{', '.join(est['inner'])}")
        if len(names) > 2:
            lines.append(f"(the map shows {names[0]} x {names[1]})")
        self.info.setText("\n".join(lines))

    def preview_mask(self) -> None:
        """The mask a FILE gives, on the scan's grid, drawn small: grey = the
        source reading, amber = measured. The first two scouted axes, X (the
        first) to the right and Y up."""
        from scan_core import scout as S
        try:
            recipe = self._recipe_fn()
            spec = S.spec_of(recipe.scout or {})
            if not recipe.scout or not spec["from"]:
                raise ValueError("choose a file first")
            dims = recipe.compile(self.registry).dims
            ks = S.scout_dims(spec, dims)
            if len(ks) != 2:
                raise ValueError("the preview draws two scouted axes")
            coarse, V = S.load_source(spec, dims, ks)
            fine = [np.asarray(dims[k].coord, float) for k in ks]
            steps = []
            for c, f in zip(coarse, fine):
                pf = S._pitch(f)
                steps.append(S._pitch(c) / pf if pf > 0 else 0.0)
            names = [dims[k].name for k in ks]
            res = S.build(spec, coarse, V, fine, S.margin_radii(spec, names, steps))
            kx, ky = S.picture_xy(spec, dims)
        except Exception as exc:
            self.picture.clear()
            self.info.setText(f"no preview: {exc}")
            return
        keep, vals = res.keep, res.fine_values
        if kx < ky:                                   # make it [y, x]
            keep, vals = keep.T, vals.T
        cy = np.asarray(dims[ky].coord, float)
        cx = np.asarray(dims[kx].coord, float)
        # Y up and X right, whichever way the axes were swept
        if cy[0] < cy[-1]:
            keep, vals = keep[::-1], vals[::-1]
        if cx[0] > cx[-1]:
            keep, vals = keep[:, ::-1], vals[:, ::-1]
        finite = np.isfinite(vals).any()
        lo = np.nanmin(vals) if finite else 0.0
        hi = np.nanmax(vals) if finite else 1.0
        grey = np.nan_to_num((vals - lo) / ((hi - lo) or 1.0), nan=0.5)
        grey = (40 + 140 * grey).astype(np.uint8)
        rgb = np.dstack([grey, grey, grey])
        acc = QtGui.QColor(C["accent"])
        a = np.array([acc.red(), acc.green(), acc.blue()], float)
        rgb[keep] = (0.45 * rgb[keep].astype(float) + 0.55 * a).astype(np.uint8)
        rgb = np.ascontiguousarray(rgb)
        h, w = keep.shape
        img = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888).copy()
        self.picture.setPixmap(QtGui.QPixmap.fromImage(img).scaled(
            self.PREVIEW_PX - 4, self.PREVIEW_PX - 4, QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.FastTransformation))
        n, total = int(res.keep.sum()), int(res.keep.size)
        self.info.setText(
            f"{n} of {total} points measured ({100.0 * n / max(1, total):.0f} %)"
            f"   ·   threshold {res.threshold:.6g}"
            f"   ·   X right, Y up")


#: the ON THE SCAN SERVER card's queue/definition tree never grows past this
SERVER_TREE_MAX = 230


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

    #: the definition on the Scan tab was edited (not emitted while a watched
    #: lab's definition is being shown -- apply_definition)
    definition_changed = QtCore.Signal()

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
        #: Set by the suite to its Lab: offers the "Clear fault on <module>"
        #: buttons of the PAUSED banner (`can_clear_fault(name)`,
        #: `clear_fault(name)`). None = no buttons (simulator, standalone).
        self.fault_lab = None
        #: Set by the suite to its Lab (None = simulator): "Recall settings..."
        #: compares a file's instrument snapshot with these live instruments.
        self.lab = None
        #: The faults the running scan is paused on ([] = not paused).
        self.paused_faults: list = []
        #: Where finished (and part-finished) scans are written without being
        #: asked. Set by the suite from its data directory; None = no autosave,
        #: which is what the standalone builder and the tests want.
        self.autosave_dir = None
        self.last_saved: Path | None = None
        #: Set by the suite to its log: routine messages ("before_scan: set
        #: mag2d.field = 150 mT ... done") land there, and in `run_log`.
        self.on_log = None
        self.run_log: list[str] = []
        #: WHERE the running scan is (see run_status_text): the engine's
        #: where_of() of the last point, (done, total, measured eta_s), and the
        #: routine step in progress ("" = none). All None/"" when idle.
        self._running = False
        self.run_where: dict | None = None
        self.run_progress: tuple | None = None
        self.run_now = ""
        #: the summary's detail line in three parts (before the ETA, the
        #: pre-run ETA clause, after it) -- so the ETA alone can be swapped for
        #: the measured remaining time while a scan runs. See _show_detail.
        self._detail_parts: tuple[str, str, str] | None = None
        self._detail_shown = ""
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
        #: replaced by ("routine", when) / ("throughout", section) placeholders.
        #: Hooks the UI does not model (a wait_ms before every point, a routine
        #: with an on_error before the scan) are kept here and written back on
        #: save, in their own place -- a definition must not lose part of itself
        #: by passing through the UI.
        self._hook_template: list = []
        #: A SCAN SERVER this run pane is showing (apps/scan_server_view.py,
        #: ServerWatch), or None: scans run in this window, exactly as before.
        #: While one is attached, the pane shows the SERVER's scan -- progress,
        #: live map, banners -- and Abort / Stop queue / the banners' buttons go
        #: to the server. Run submits there only when `server_submit` is True:
        #: on the server's own PC with the suite's "Run scans on this PC's scan
        #: server" setting on; on ANOTHER PC (phase 2, 2026-10-10) while this
        #: suite holds control of the server, or nobody does.
        self.server = None
        self._may_submit_flag = False
        #: True when the attached server runs on THIS PC (this suite is the
        #: lab's own: it publishes its view, and has no queue card to mirror)
        self._server_local = False
        #: the host's way of copying a server file to this PC and opening it
        #: (suite: lab_files.FileFetcher -> Data tab); None = not offered
        self.on_fetch_file = None
        self._server_faults = None
        self._server_answered: tuple[str, float] = ("", 0.0)
        #: the host's part of a published view (suite: the Control tab's panel)
        #: and its handler for a watched one -- None in the standalone builder
        self.view_extra = None
        self.on_server_view = None

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
        if hasattr(self, "window_card"):
            self.window_card.load_block(None)
            self.window_card.set_registry(registry)
        if hasattr(self, "scout_section"):
            self.scout_section.load_block(None)
            self.scout_section.set_registry(registry)
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
                "Add this parameter as the last step of the " + when.replace("_", "-")
                + " ROUTINE.\nThe steps run top to bottom, each waited for (a set\n"
                "until it has settled, an action such as a VNA reference until\n"
                "it has finished), "
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
        # a second, narrow column: what ONE scan point records -- 0D (a number),
        # 1D (a trace: a spectrum, a VNA sweep), 2D (an image). Lukas
        # 2026-10-06: "0D, 1D, 2D to visualize what is acquired"
        self.det_tree.setColumnCount(2)
        hdr = self.det_tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
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
            or next((p.id for p in gettables
                     if getattr(p, "dtype", "") not in ("text", "enum", "string")), None)
        for module, params in group_by_module(gettables).items():
            group = self._group_item(self.det_tree, module, len(params))
            for p in params:
                tag, tip = detector_shape(p)
                it = QtWidgets.QTreeWidgetItem(group, [self._param_text(p), tag])
                it.setData(0, QtCore.Qt.UserRole, p.id)
                it.setToolTip(0, p.id)
                it.setToolTip(1, tip)
                it.setForeground(1, QtGui.QBrush(QtGui.QColor(
                    C["accent"] if tag != "0D" else C["muted"])))
                it.setTextAlignment(1, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(0, QtCore.Qt.Checked if p.id == default
                                 else QtCore.Qt.Unchecked)
            group.setExpanded(True)
        self.det_tree.blockSignals(False)
        if self.routines:                    # not built yet on the first call
            self._refresh_routine_actions()

    def _sync_fly_detectors(self) -> int:
        """With a FLY axis in the stack, grey out the detectors that cannot fly.

        A fly scan records its detectors continuously and bins them by
        position, so only a detector its module can STREAM qualifies -- not a
        one-value-at-a-time read. A whole 1-D trace qualifies when its module
        streams every sweep (the VNA, 2026-10-09): it is binned trace by
        trace; one with more dimensions of its own does not. Rather than
        let a ticked one turn the whole scan "invalid", it is unticked and
        greyed while any axis flies, and REMEMBERED: switch fly off and it is
        ticked again, so trying fly on and off does not lose a detector
        selection. Returns how many are set aside.
        """
        fly = any((r.raw or {}).get("type") == "fly" if r.raw is not None
                  else r.is_fly() for r in self.rows)
        parked = self.__dict__.setdefault("_fly_parked", set())
        self.det_tree.blockSignals(True)        # itemChanged would call us again
        try:
            for it in self._det_items():
                pid = it.data(0, QtCore.Qt.UserRole)
                p = self.registry.get(pid) if pid else None
                ok = (not fly) or (p is not None and getattr(p, "stream", None) is not None
                                   and len(getattr(p, "axes", None) or []) <= 1)
                if not ok:
                    if it.checkState(0) == QtCore.Qt.Checked:
                        parked.add(pid)
                        it.setCheckState(0, QtCore.Qt.Unchecked)
                    if not it.isDisabled():
                        it.setDisabled(True)
                        why = ("it has more than one dimension of its own"
                               if len(getattr(p, "axes", None) or []) > 1
                               else "its module does not stream it")
                        it.setToolTip(0, f"{pid}" + chr(10) + f"cannot be recorded in a FLY scan: {why}")
                else:
                    if it.isDisabled():
                        it.setDisabled(False)
                        it.setToolTip(0, pid)
                    if pid in parked:
                        parked.discard(pid)
                        it.setCheckState(0, QtCore.Qt.Checked)
        finally:
            self.det_tree.blockSignals(False)
        self.det_tree.viewport().update()
        return len(parked) if fly else 0

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
        rep = QtWidgets.QPushButton("＋ Repeat")
        rep.setToolTip("Add a REPEAT row: everything below it is done N times.\n"
                       "On top: whole scans; at the bottom: every point N times.\n"
                       "Keep every repeat, or store only their average.")
        rep.clicked.connect(lambda: self.add_repeat())
        head.addWidget(rep)
        v.addLayout(head)

        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        holder = QtWidgets.QWidget(); holder.setObjectName("root")
        self.stack_lay = QtWidgets.QVBoxLayout(holder)
        self.stack_lay.setContentsMargins(0, 0, 0, 0); self.stack_lay.setSpacing(8)
        self.stack_lay.addStretch(1)
        scroll.setWidget(holder)
        self.axis_scroll = scroll
        v.addWidget(scroll, 1)
        # The SCOUT PASS: always here, one line when closed -- never a card
        # that pops in with the second axis and squeezes the layout (Lukas,
        # 2026-10-08). Opened, it takes the room from the axis list above.
        self.scout_section = ScoutSection(recipe_fn=self.build_recipe)
        self.scout_section.set_registry(self.registry)
        self.scout_section.changed.connect(self._rebuild_summary)
        self.scout_section.expanded_changed.connect(self._size_axis_list)
        v.addWidget(self.scout_section, 0)
        self._size_axis_list(False)
        return card

    #: The axis list always shows at least this many axis rows -- also with
    #: the scout section open: the axes the scout is about must stay in view
    #: (the first render of the open section showed one row and a scroll bar).
    AXIS_ROWS_MIN = 2.5
    #: one axis row plus the spacing between rows, in pixels
    AXIS_ROW_PX = 64

    def _size_axis_list(self, open_: bool) -> None:
        """Keep the axis-stack card the SAME height open or closed, so nothing
        below it moves when the scout section is toggled: closed, the axis
        list's minimum includes the room the open body would take; open, the
        body takes that room and the list keeps its 2.5 rows. If the window is
        too short for that, the middle column scrolls (_build_middle) rather
        than squeezing the axis list."""
        rows = int(self.AXIS_ROWS_MIN * self.AXIS_ROW_PX)
        body = self.scout_section.BODY_MAX + self.scout_section.layout().spacing()
        self.axis_scroll.setMinimumHeight(rows if open_ else rows + body)

    def _build_middle(self) -> QtWidgets.QWidget:
        """The axis stack, with the conditions underneath it.

        Two cards, not two tabs: what is swept and what is held are one
        description of one measurement, and hiding half of it behind a tab is
        how a scan gets run at last week's RF power.
        """
        page = QtWidgets.QWidget(); page.setObjectName("root")
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(12)
        v.addWidget(self._build_stack(), 1)
        v.addWidget(self._build_conditions(), 0)
        v.addWidget(self._build_routines(), 0)
        # RESONANCE WINDOW: hidden until a ticked detector supports one
        self.window_card = WindowCard()
        self.window_card.set_registry(self.registry)
        self.window_card.changed.connect(self._rebuild_summary)
        v.addWidget(self.window_card, 0)
        # The column SCROLLS when the window is too short for all of it
        # (the axis list keeps its 2.5 rows, the cards their own minimum)
        # instead of squeezing whichever card gives way first.
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        area.setWidget(page)
        self.middle_scroll = area
        return area

    def _build_routines(self) -> QtWidgets.QWidget:
        """ROUTINES: what happens once before the scan and once after it.

        Below the conditions because it is read in the same breath: what is
        swept, what is held, and what is done around it. The VNA-FMR case is
        the reason it exists -- go to a far-off field, take a reference, sweep
        the field, then field -> 0.
        """
        card = RoutinesCard()          # sizes itself: side by side or stacked
        self.routines_card = card
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
            section.set_registry(self.registry)
            section.set_actions(actions)
            section.changed.connect(self._rebuild_summary)
            self.routines[when] = section
            card.box.addWidget(section, 1)
        card.box.addWidget(self._build_throughout(actions), 1)
        return card

    def _build_throughout(self, actions) -> QtWidgets.QWidget:
        """THROUGHOUT: any number of routines that fire during the scan."""
        col = _ThroughoutColumn(lambda: self.throughout,
                                lambda: getattr(self, "throughout_scroll", None))
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
        self.throughout_scroll = scroll
        self._throughout_actions = actions
        return col

    def add_throughout(self, hook: dict | None = None) -> ThroughoutSection:
        """A new THROUGHOUT routine; `hook` fills its trigger from a recipe."""
        section = ThroughoutSection()
        section.set_registry(self.registry)
        section.set_actions(self._throughout_actions)
        names = self._dim_names()
        section.set_dims(names, self._dim_labels(names))
        if hook:
            section.set_trigger(hook.get("when"), axis=hook.get("axis"),
                                edge=hook.get("edge"), every=hook.get("every", 1),
                                n=hook.get("n"),
                                on_error=hook.get("on_error"))
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
        """Append "set `pid`" to a THROUGHOUT routine (`section`, else the active one,
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
            section.set_registry(self.registry)
            section.set_actions(actions)
        self._throughout_actions = actions
        for section in self.throughout:
            section.set_registry(self.registry)
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

    def _build_server_box(self) -> QtWidgets.QWidget:
        """WATCHING a scan server on another PC (Lukas 2026-10-06: "a 1:1 copy
        of what i see on the lab pc"): the submitted queue with each scan's
        definition and run info, read-only; the lab's plot choice followed on
        request; a definition copied into this PC's Scan tab on request."""
        box = QtWidgets.QFrame(); box.setObjectName("card")
        v = QtWidgets.QVBoxLayout(box); v.setContentsMargins(10, 8, 10, 8); v.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(self._tag("ON THE SCAN SERVER"))
        head.addStretch(1)
        self.follow_view_box = QtWidgets.QCheckBox("show what the lab shows")
        self.follow_view_box.setChecked(True)
        self.follow_view_box.setToolTip(
            "On: the plot below follows the choice made in the measurement suite\n"
            "on the scan server's PC (detector, X / Y, held slices, colour range).\n"
            "Off: choose your own view here; the lab's screen is not affected\n"
            "either way.")
        self.follow_view_box.toggled.connect(self._follow_view_toggled)
        head.addWidget(self.follow_view_box)
        self.copy_def_btn = QtWidgets.QPushButton("Copy to Scan tab")
        self.copy_def_btn.setToolTip(
            "Load the selected scan's definition into this PC's Scan tab, to\n"
            "reuse or adapt it here. Parameters of modules this PC is not\n"
            "connected to are named, as with Load scan...")
        self.copy_def_btn.clicked.connect(self._copy_server_definition)
        self.copy_def_btn.setEnabled(False)
        head.addWidget(self.copy_def_btn)
        v.addLayout(head)
        # PHASE 2 (2026-10-10): edit the RUNNING queue from here -- add this
        # Scan tab's definition, remove / move a scan that has not started --
        # and copy a finished scan's file to this PC. The edits need control
        # of the server (the server checks; the buttons only follow it).
        qrow = QtWidgets.QHBoxLayout()
        self.queue_add_btn = QtWidgets.QPushButton("+ Add to queue")
        self.queue_add_btn.setToolTip(
            "Add the definition on THIS PC's Scan tab to the end of the running\n"
            "queue, with this PC's run info (sample, operator, ...). Checked\n"
            "against the scan server's instruments and limits first.\n"
            "Needs control of the scan server (or nobody holding it).")
        self.queue_add_btn.clicked.connect(self._queue_add_clicked)
        self.queue_remove_btn = QtWidgets.QPushButton("Remove")
        self.queue_remove_btn.setToolTip(
            "Take the selected scan out of the queue. Only a scan that has not\n"
            "started: the running one is ended with Abort.")
        self.queue_remove_btn.clicked.connect(self._queue_remove_clicked)
        self.queue_up_btn = QtWidgets.QPushButton("Up")
        self.queue_up_btn.setToolTip("Run the selected waiting scan one place earlier.")
        self.queue_up_btn.clicked.connect(lambda: self._queue_move_clicked(-1))
        self.queue_down_btn = QtWidgets.QPushButton("Down")
        self.queue_down_btn.setToolTip("Run the selected waiting scan one place later.")
        self.queue_down_btn.clicked.connect(lambda: self._queue_move_clicked(+1))
        self.fetch_btn = QtWidgets.QPushButton("Copy to this PC")
        self.fetch_btn.setToolTip(
            "Copy the selected finished scan's file (or, with none selected, the\n"
            "last one saved) from the scan server's PC to this PC, and open it in\n"
            "the Data tab. The original stays where it was saved.")
        self.fetch_btn.clicked.connect(self._fetch_clicked)
        for b in (self.queue_add_btn, self.queue_remove_btn, self.queue_up_btn,
                  self.queue_down_btn):
            qrow.addWidget(b)
        qrow.addStretch(1)
        qrow.addWidget(self.fetch_btn)
        v.addLayout(qrow)
        self.server_tree = QtWidgets.QTreeWidget()
        self.server_tree.setHeaderHidden(True)
        self.server_tree.setRootIsDecorated(True)
        self.server_tree.itemExpanded.connect(lambda *_: self._size_server_tree())
        self.server_tree.itemCollapsed.connect(lambda *_: self._size_server_tree())
        self.server_tree.setToolTip("Every scan the server was given, in order. Open one\n"
                                    "(the arrow) to read its run info and definition.")
        self.server_tree.currentItemChanged.connect(lambda *_: self._sync_queue_buttons())
        v.addWidget(self.server_tree)
        box.hide()
        self.server_box = box
        self._server_scan: dict = {}
        self._server_tree_key = None
        self._view_published = False      # view_changed -> _publish_view connected
        return box

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
        self.name_lbl = QtWidgets.QLabel("name")
        nrow.addWidget(self.name_lbl)
        self.name_edit = QtWidgets.QLineEdit("scan")
        self.name_edit.setToolTip(
            "Goes into the file name: <data dir>\\<date>\\<time>_<name>.nc\n"
            "The time is always there, so repeating a scan never overwrites the\n"
            "one before it. Characters a file name cannot hold become '_'.")
        # wide enough for a real name ("Harmonics12GHz_vernier_series") without
        # scrolling inside the box (Lukas 2026-10-06: "wider")
        self.name_edit.setMinimumWidth(420)
        self.name_edit.setMaximumWidth(700)
        self.name_edit.textChanged.connect(lambda *_: self._refresh_save_target())
        nrow.addWidget(self.name_edit)
        nrow.addStretch(1)
        v.addLayout(nrow)

        # RUN INFO: sample, operator, project ... -- remembered on this PC and
        # written into every file (scan_core/run_info.py). Collapsed to one
        # line so the run pane stays as it was.
        self.run_info = RunInfoCard()
        v.addWidget(self.run_info)

        row = QtWidgets.QHBoxLayout()
        self.per_pt_lbl = QtWidgets.QLabel("per-point (s)")
        row.addWidget(self.per_pt_lbl)
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
        self.diagonal_box = QtWidgets.QCheckBox("diagonal")
        self.diagonal_box.setToolTip(
            "At the start of a new row, send BOTH new coordinates and then wait,\n"
            "so the stage (or the camera's stabiliser) goes straight to the first\n"
            "point of the next row -- instead of first settling at (last column,\n"
            "next row), one wasted settle per row.\n\n"
            "Off by default: two moves at once must be allowed by the hardware.\n"
            "Fine for the camera's array point (one target); a KIM101 moving two\n"
            "channels together is not verified yet.")
        self.diagonal_box.toggled.connect(lambda *_: self._rebuild_summary())
        row.addWidget(self.diagonal_box)
        row.addStretch(1)
        self.run_btn = QtWidgets.QPushButton("▶  Run scan"); self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run_scan)
        # PAUSE (Lukas, 2026-10-07): hold the scan between two points -- to
        # refill a dewar, look at the sample, lend the magnet for a minute --
        # without ending it. No objectName: the plain button of the theme, so it
        # does not compete with Run (amber) and Abort (red).
        self.pause_btn = QtWidgets.QPushButton(self.PAUSE_TEXT)
        self.pause_btn.setToolTip(self.PAUSE_TIP)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.pause_btn.setEnabled(False)
        #: True while THIS pane has the scan paused (or, watching a scan
        #: server, while the server reports user_paused)
        self._user_paused = False
        #: (state, deadline) of the last Pause / Resume click on a server scan
        self._pause_click: tuple | None = None
        self.abort_btn = QtWidgets.QPushButton("■ Abort"); self.abort_btn.setObjectName("danger")
        self.abort_btn.clicked.connect(self._abort); self.abort_btn.setEnabled(False)
        self.stop_queue_btn = QtWidgets.QPushButton("■■ Stop queue")
        self.stop_queue_btn.setObjectName("danger")
        self.stop_queue_btn.setToolTip("Abort the scan that is running AND every scan after it.\n"
                                       "(Abort alone skips to the next scan.)")
        self.stop_queue_btn.clicked.connect(self.stop_queue)
        self.stop_queue_btn.hide()
        row.addWidget(self.run_btn); row.addWidget(self.pause_btn)
        row.addWidget(self.abort_btn); row.addWidget(self.stop_queue_btn)
        v.addLayout(row)

        # The PAUSED banner: a fault (a camera that lost its pattern, a failed
        # hardware read, a service that went silent) stops the scan BEFORE a
        # wrong number is recorded, and this says why and what to do. The scan
        # resumes by itself once every fault is gone, and measures the point
        # again; Abort (above) still ends it.
        self.pause_box = QtWidgets.QFrame(); self.pause_box.setObjectName("card")
        self.pause_box.setStyleSheet(
            f"QFrame#card {{ border: 2px solid {C['danger']}; border-radius: 6px; }}")
        pb = QtWidgets.QVBoxLayout(self.pause_box)
        pb.setContentsMargins(10, 8, 10, 8)
        self.pause_title = QtWidgets.QLabel("PAUSED -- the scan is waiting")
        self.pause_title.setStyleSheet(f"color:{C['danger']}; font-weight:800; font-size:14px;")
        pb.addWidget(self.pause_title)
        self.pause_lbl = QtWidgets.QLabel("")
        self.pause_lbl.setWordWrap(True)
        self.pause_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        pb.addWidget(self.pause_lbl)
        self.pause_hint = QtWidgets.QLabel(
            "Fix the cause; the scan resumes by itself and measures this point "
            "again. Abort ends the scan (the points so far are kept).")
        self.pause_hint.setWordWrap(True)
        self.pause_hint.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
        pb.addWidget(self.pause_hint)
        self.pause_btns = QtWidgets.QHBoxLayout()
        self.pause_btns.addStretch(1)            # buttons go in before it
        pb.addLayout(self.pause_btns)
        self.clear_fault_btns: dict = {}         # module name -> its button
        self.pause_box.hide()
        v.addWidget(self.pause_box)

        # The OPERATOR banner (2026-10-04): a `pause` step in a routine --
        # "insert the polariser, then Continue". The scan waits here until it
        # is answered; Abort scan stops it like abort_if (data kept, the reason
        # in the file). Amber, not red: nothing is wrong, it is your turn.
        self.ask_box = QtWidgets.QFrame(); self.ask_box.setObjectName("card")
        self.ask_box.setStyleSheet(
            f"QFrame#card {{ border: 2px solid {C['accent']}; border-radius: 6px; }}")
        ab = QtWidgets.QVBoxLayout(self.ask_box)
        ab.setContentsMargins(10, 8, 10, 8)
        ask_title = QtWidgets.QLabel("YOUR TURN -- the scan is waiting for you")
        ask_title.setStyleSheet(f"color:{C['accent']}; font-weight:800; font-size:14px;")
        ab.addWidget(ask_title)
        self.ask_lbl = QtWidgets.QLabel("")
        self.ask_lbl.setWordWrap(True)
        self.ask_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.ask_lbl.setStyleSheet("font-size:13px;")
        ab.addWidget(self.ask_lbl)
        arow = QtWidgets.QHBoxLayout()
        arow.addStretch(1)
        self.ask_continue_btn = QtWidgets.QPushButton("Continue")
        self.ask_continue_btn.setObjectName("primary")
        self.ask_continue_btn.clicked.connect(lambda: self.answer_pause(True))
        self.ask_abort_btn = QtWidgets.QPushButton("Abort scan")
        self.ask_abort_btn.setObjectName("danger")
        self.ask_abort_btn.setToolTip("Stop the scan here: the after-scan routine runs, the\n"
                                      "points so far are saved, the file says why.")
        self.ask_abort_btn.clicked.connect(lambda: self.answer_pause(False))
        self.ask_abort_all_btn = QtWidgets.QPushButton("Abort all")
        self.ask_abort_all_btn.setObjectName("danger")
        self.ask_abort_all_btn.setToolTip("Stop this scan AND the rest of the queue: the "
                                          "after-scan routine runs, the points so far are saved.")
        self.ask_abort_all_btn.clicked.connect(lambda: self.answer_pause("all"))
        arow.addWidget(self.ask_continue_btn); arow.addWidget(self.ask_abort_btn)
        arow.addWidget(self.ask_abort_all_btn)
        ab.addLayout(arow)
        self._ask_answer = None
        self.ask_box.hide()
        v.addWidget(self.ask_box)

        # "Scan 2 of 5 · name" while a QUEUE runs; its summary when it ends.
        self.queue_lbl = QtWidgets.QLabel("")
        self.queue_lbl.setStyleSheet(f"color:{C['accent']}; font-weight:700;")
        self.queue_lbl.setWordWrap(True)
        self.queue_lbl.hide()
        v.addWidget(self.queue_lbl)

        # WHERE the running scan is (run_status_text). In the suite the same
        # line sits in the Measurement tab's header, next to RUNNING, so this
        # copy is only shown in the standalone builder.
        self.where_lbl = QtWidgets.QLabel("")
        self.where_lbl.setStyleSheet(f"color:{C['accent']};")
        self.where_lbl.setWordWrap(True)
        self.where_lbl.hide()
        v.addWidget(self.where_lbl)

        self.progress = QtWidgets.QProgressBar(); self.progress.setValue(0)
        v.addWidget(self.progress)
        self.save_lbl = QtWidgets.QLabel(""); self.save_lbl.setObjectName("hint")
        self.save_lbl.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
        self.save_lbl.setWordWrap(True)
        v.addWidget(self.save_lbl)
        v.addWidget(self._build_server_box())

        # Result: the general N-D viewer, not a fixed pair of axes. The same
        # widget serves the Data tab, so what you watch during a run behaves
        # exactly like what you open a saved file with.
        self.view = DataView()
        # the newest CAMERA frame next to the map, shown only while a scan
        # records an image detector (apps/image_live.py, 2026-10-10)
        self.live_image = LiveImage()
        res_row = QtWidgets.QHBoxLayout()
        res_row.setSpacing(8)
        res_row.addWidget(self.view, 1)
        res_row.addWidget(self.live_image)
        v.addLayout(res_row, 1)
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
        recall = QtWidgets.QPushButton("Recall settings…")
        recall.clicked.connect(self._recall_dialog)
        recall.setToolTip("Compare the instrument settings stored in a measured .nc\n"
                          "with the instruments' settings NOW, and set chosen ones\n"
                          "back. Nothing is sent before you confirm.")
        self.recall_btn = recall
        brow.addWidget(load); brow.addWidget(save); brow.addWidget(recall)
        brow.addStretch(1); brow.addWidget(savd)
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
        # a NEW row starts from the module's CURRENT limits (a camera array
        # resized since the last refresh would otherwise be offered as it was)
        if self.limits_refresher is not None:
            try:
                self.limits_refresher()
            except Exception:
                pass                     # a dead service must not block adding
        p = self.registry.get(pid)
        sp = find_speed_param(self.registry, pid)
        # A streamed coordinate with no speed knob of its own is MEASURED, not
        # driven (camera.laser_x): offer the stages that could fly it -- same
        # unit, another module, with a speed knob.
        choices = []
        if sp is None and p is not None and getattr(p, "stream", None) is not None:
            own = pid.rsplit(".", 1)[0] if "." in pid else ""
            choices = [q.id for q in self.registry.settables()
                       if q.id != pid and q.unit == p.unit
                       and (q.id.rsplit(".", 1)[0] if "." in q.id else "") != own
                       and find_speed_param(self.registry, q.id)]

        def lookup(mid, _reg=self.registry):
            s_id = find_speed_param(_reg, mid) if mid else None
            return _reg.get(s_id) if s_id else None

        row = AxisRow(p, self._level_of,
                      speed_param=self.registry.get(sp) if sp else None,
                      move_choices=choices, speed_lookup=lookup,
                      registry=self.registry)
        row.raw = raw
        if hasattr(self, "zigzag_box"):
            row.set_zigzag(self.zigzag_box.isChecked())
        row.changed.connect(self._rebuild_summary)
        row.remove.connect(self._remove_row)
        row.move.connect(self._move_row)
        row.preview.connect(self.preview_row)
        self._wire_advanced(row)
        row.zigzag_changed.connect(self._zigzag_from_row)
        self.rows.append(row)
        self.stack_lay.insertWidget(self.stack_lay.count() - 1, row)  # before stretch
        self._relevel(); self._rebuild_summary()

    def add_repeat(self, num: int = 5, mode: str = "keep",
                   interval_s: float | None = None, name: str | None = None,
                   index: int | None = None) -> RepeatRow:
        """Add a REPEAT row (scan_core/repeat.py) -- at the bottom of the stack,
        or at `index` (0 = outermost)."""
        row = RepeatRow(self._level_of, num=num, mode=mode,
                        interval_s=interval_s, name=name)
        row.changed.connect(self._rebuild_summary)
        row.remove.connect(self._remove_row)
        row.move.connect(self._move_row)
        self._wire_advanced(row)
        if index is None or not 0 <= index < len(self.rows):
            self.rows.append(row)
            self.stack_lay.insertWidget(self.stack_lay.count() - 1, row)
        else:
            self.rows.insert(index, row)
            self.stack_lay.insertWidget(index, row)
        self._relevel(); self._rebuild_summary()
        return row

    # ---- the Advanced panels of the axis rows -----------------------------
    def _wire_advanced(self, row) -> None:
        row.advanced_toggled.connect(self._advanced_toggled)
        row.log.connect(self._advanced_log)

    def _advanced_toggled(self, row, on: bool) -> None:
        """Only ONE Advanced panel open at a time (the approved design): two
        open panels would push the axes being edited out of the small axis
        list. The opened one is scrolled into view, its row line first."""
        if not on:
            return
        for r in self.rows:
            if r is not row and r.advanced_open():
                r.set_advanced_open(False)

        def show(r=row, tries=6):
            # the row line at the TOP of the axis list: the panel under it
            # then gets all the room there is (the list is only ~2.5 rows
            # tall). The scroll range grows only once Qt has laid the opened
            # panel out, so try again a few times until it reaches the row.
            if r not in self.rows or not r.advanced_open():
                return
            bar = self.axis_scroll.verticalScrollBar()
            want = max(0, r.y() - 2)
            bar.setValue(min(bar.maximum(), want))
            if bar.value() < want and tries > 0:
                QtCore.QTimer.singleShot(40, lambda: show(r, tries - 1))
        QtCore.QTimer.singleShot(0, show)

    def open_advanced(self, row) -> None:
        """Open `row`'s Advanced panel (and close any other)."""
        row.set_advanced_open(True)

    def _advanced_log(self, msg: str) -> None:
        if self.on_log is not None:
            self.on_log(msg)

    def _zigzag_from_row(self, on: bool) -> None:
        """A fly row's direction box IS the scan's zig-zag: one setting, two
        places to reach it (the run pane, and the fly axis it matters most for)."""
        if self.zigzag_box.isChecked() != bool(on):
            self.zigzag_box.setChecked(bool(on))

    def _axis_routines(self, recipe) -> list[list[str]]:
        """Per row, the routines bound to its axis (each_sweep / before_axis /
        after_axis naming one of its dims), as short lines for POINT."""
        from scan_core.hooks import describe_trigger, routine_steps
        from scan_core.recipe import _compile_axis
        out = []
        dims_of = []
        try:
            for r in self.rows:
                ax = r.to_axis()
                n = len(_compile_axis(ax))
                dims_of.append(n)
            names = [d.name for d in recipe.compile(self.registry).dims]
        except Exception:
            return [[] for _ in self.rows]
        k = 0
        for r, n in zip(self.rows, dims_of):
            mine = set(names[k:k + n]); k += n
            lines = []
            for h in recipe.hooks or []:
                if not isinstance(h, dict) or h.get("axis") not in mine or \
                        h.get("when") not in ("each_sweep", "before_axis", "after_axis"):
                    continue
                what = h.get("action")
                if what == "call":
                    try:
                        parts = [f"set {s[1]}" if s[0] == "set" else
                                 f"run {s[1]}" if s[0] == "action" else s[0]
                                 for s in routine_steps(h.get("args") or {})]
                        what = ", ".join(parts) or "call"
                    except ValueError:
                        what = "call"
                trig = (describe_trigger(h) if h.get("when") == "each_sweep"
                        else f"{h['when'].replace('_', ' ')} {h.get('axis')}")
                lines.append(f"{trig}: {what}")
            out.append(lines)
        return out

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
        """Append "set `pid` = `value`" to the before_scan / after_scan routine
        (or update it, if it is already set after the routine's last action).

        None if the registry has no SETTABLE of that id (the routine could not
        set it, so the row would be a promise nothing keeps).
        """
        p = self.registry.get(pid)
        if p is None or getattr(p, "kind", "") != "settable" or when not in self.routines:
            return None
        return self.routines[when].add_set(p, value)

    def add_routine_action(self, when: str, aid: str):
        """Append "run `aid`" as the LAST step of the before_scan / after_scan
        routine. None if no connected module offers that action."""
        if when not in self.routines:
            return None
        return self.routines[when].add_action(aid)

    def set_routine_action(self, when: str, aid: str | None) -> bool:
        """The one-action API: make `aid` the routine's only action, after its
        sets (None = no action). False if unknown. add_routine_action() adds
        one more instead."""
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
                      # ONE comment: the run info's is the recipe's
                      comment=self.run_info.values()["comment"],
                      zigzag=self.zigzag_box.isChecked(),
                      diagonal=self.diagonal_box.isChecked(),
                      window=(self.window_card.to_block()
                              if hasattr(self, "window_card") else None),
                      scout=(self.scout_section.to_block()
                             if hasattr(self, "scout_section") else None))

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

        Every plain `call` hook becomes STEPS on the card, in either spelling
        ({set, action} or {steps: [...]}, see hooks.routine_steps):

        * BEFORE / AFTER SCAN show one ordered list each. Several call hooks at
          the same moment -- how an older definition wrote "set A, run X, then
          set B, run Y" -- are joined into that one list, in their order, as
          long as no OTHER hook firing at that moment sits between them (that
          hook would then run in the middle, and the list could not say so; it
          is kept verbatim instead). Saved back, they become one routine, whose
          restore runs once at the end rather than after each part; the state
          the scan is left in is the same (hooks._call).
        * THROUGHOUT: one section per hook, as before (two sections may share
          a trigger on purpose).

        Anything else -- other actions, other moments, a call with keys the
        card cannot show (an on_error before the scan) or malformed args -- is
        kept verbatim and saved back unchanged, in its place.

        Missing parameters and actions are FLAGGED, in every call hook, exactly
        like a missing axis: a definition written against the lab must not come
        back on the simulator quietly skipping its reference.
        """
        from scan_core.hooks import STEP_KINDS, routine_steps
        missing: list[str] = []
        for section in self.routines.values():
            section.clear()
        for section in list(self.throughout):
            self.remove_throughout(section)
        self._hook_template = []
        # moment -> what the last hook that fires at that moment was: "routine"
        # (the card's list, so a following call hook can join it) or "other".
        last_at: dict[str, str] = {}
        placed: set[str] = set()                   # moments whose list has its place
        get_action = getattr(self.registry, "get_action", None)
        for h in hooks or []:
            if not isinstance(h, dict):
                self._hook_template.append(h)
                continue
            when = h.get("when")
            args = h.get("args") if isinstance(h.get("args"), dict) else None
            steps = None
            if h.get("action") == "call" and args is not None:
                try:
                    steps = routine_steps(args)
                except ValueError:
                    steps = None            # malformed: kept verbatim, validate() says why
            if steps is not None:
                # Flag what is missing whether or not the card can show it.
                for kind, ident, *_ in steps:
                    if kind == "set":
                        p = self.registry.get(ident)
                        if p is None or getattr(p, "kind", "") != "settable":
                            missing.append(ident)
                    elif kind in STEP_KINDS:
                        missing += _generic_missing(kind, ident, self.registry)
                    elif get_action is None or get_action(ident) is None:
                        missing.append(ident)
            if (steps is not None and when in THROUGHOUT_WHENS
                    and set(h) <= THROUGHOUT_KEYS):
                section = self.add_throughout(h)
                self._hook_template.append(("throughout", section))
                section.load_args(args, self.registry)
                continue
            modelled = (steps is not None and when in self.routines
                        and set(h) <= {"when", "action", "args"}
                        and (when not in placed or last_at.get(when) == "routine"))
            if not modelled:
                self._hook_template.append(copy.deepcopy(h))
                if when:
                    last_at[when] = "other"
                continue
            if when not in placed:                    # the first part: its place
                self._hook_template.append(("routine", when))
                placed.add(when)
            last_at[when] = "routine"
            self.routines[when].load_args(args, self.registry)
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
        self.diagonal_box.blockSignals(True)
        self.diagonal_box.setChecked(bool(getattr(recipe, "diagonal", False)))
        self.diagonal_box.blockSignals(False)
        if getattr(recipe, "name", ""):
            self.name_edit.setText(recipe.name)
        if getattr(recipe, "comment", ""):
            self.run_info.set_comment(recipe.comment)
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
            if ax.get("type") == "repeat":
                # drives no parameter, so nothing can be missing
                try:
                    num = int(ax.get("num") or 1)
                except (TypeError, ValueError):
                    num = 1
                self.add_repeat(num=num, mode=ax.get("mode") or "keep",
                                interval_s=ax.get("interval_s"),
                                name=ax.get("name"))
                continue
            pid = (ax.get("param") or ax.get("x", {}).get("param")
                   or (ax.get("members") or [{}])[0].get("param"))
            if not pid or self.registry.get(pid) is None:
                missing.append(pid or f"<{ax.get('type', 'axis')}>")
                continue
            if ax.get("type") in ("linear", "fly"):
                self.add_axis(pid)
                # every fly option has its box in Advanced now (the speed
                # knob, readback, lag correction, row timeout ...), so a fly
                # axis is no longer passed through as raw to keep them
                missing += self.rows[-1].load_axis(ax)
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
        if hasattr(self, "window_card"):
            self._sync_window_card()
            missing += [m for m in self.window_card.load_block(getattr(recipe, "window", None))
                        if m not in missing]
        if hasattr(self, "scout_section"):
            # the ticks on the rows (and their steps), then the section
            block = getattr(recipe, "scout", None)
            axes = (block or {}).get("axes") if isinstance(block, dict) else None
            if isinstance(axes, dict):
                for row in self.rows:
                    hits = [n for n in row.scout_dims() if n in axes]
                    if hits:
                        row.scout.blockSignals(True)
                        row.scout.setChecked(True)
                        row.scout.blockSignals(False)
                        row.scout_step.setEnabled(True)
                        try:
                            row.scout_step.setValue(int(axes[hits[0]]))
                        except (TypeError, ValueError):
                            pass
                        # the margin is per axis, on the row (Advanced > SCOUT)
                        row.set_scout_margin(block.get("margin", "auto"))
                ticked = {n for r in self.rows if r.is_scout() for n in r.scout_dims()}
                missing += [f"scout axis {n}" for n in axes if n not in ticked]
            elif block:
                # an old XY mask over two plain axes named no axes: there is
                # nothing to tick, and the block would silently be dropped
                missing.append("the scout's axes (tick 'scout' on the axes to scout)")
            self._sync_scout_section()
            missing += [m for m in self.scout_section.load_block(block)
                        if m not in missing]
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
        # every edit of the definition passes here: a scan server's watchers
        # are told (the suite publishes it with the view; Lukas 2026-10-06)
        if not getattr(self, "_applying_definition", False):
            self.definition_changed.emit()
        if self.queue_running():
            # The pane describes the scan that is RUNNING, not the editor's.
            return
        dim_names = self._dim_names()
        labels = self._dim_labels(dim_names)
        for section in self.throughout:
            section.set_dims(dim_names, labels)
        if hasattr(self, "throughout_empty"):
            self.throughout_empty.setVisible(not self.throughout)
        if hasattr(self, "routines_card"):
            self.routines_card.arrange()      # a new step may no longer fit side by side
        parked = self._sync_fly_detectors()
        self._sync_window_card()
        self._sync_scout_section()
        recipe = self.build_recipe()
        zig = self.zigzag_box.isChecked()
        for row, lines in zip(self.rows, self._axis_routines(recipe)):
            row.set_zigzag(zig)               # a fly row's direction box
            row.set_axis_routines(lines)      # POINT: routines on this axis
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
        if hasattr(self, "window_card") and self.window_card.describe():
            conditions += "   ·   " + self.window_card.describe()
        if hasattr(self, "scout_section") and self.scout_section.describe():
            conditions += "   ·   " + self.scout_section.describe()
        if parked:
            conditions += (f"   ·   {parked} detector(s) set aside while flying "
                           f"(they cannot be recorded continuously)")
        self._detail_parts = None            # set again below, if there is an ETA
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
        fly = fly_axis(recipe)
        if fly is not None:
            # A fly row is one move: its time is distance / speed, plus the
            # approach to its start (a settle, costed like one point).
            # The registry matters: the pace may be a row time, or (a knob its
            # module sweeps) the module's default rate -- fly_rate knows both.
            rows = max(1, n // max(1, comp.dims[-1].size))
            t_row = row_seconds(fly, self.registry)
            eta = rows * (t_row + self.per_pt.value())
            how = f"fly: {rows} row(s) × {t_row:.3g} s"
            self.summary.setText(f"{len(comp.dims)}-D   {shape} = {n:,} px (fly)")
        else:
            eta = n * self.per_pt.value()
            how = f"@ {self.per_pt.value():g}s/pt"
            self.summary.setText(f"{len(comp.dims)}-D   {shape} = {n:,} pts")
            from scan_core.scout import estimate as scout_estimate
            est = scout_estimate(recipe)
            if est is not None:
                # With a SCOUT PASS only the scout's own points are known
                # before the run; how many points follow is what the scout
                # decides. So: the scout's time, and the full grid as the
                # ceiling. (Rig, 2026-10-08: a scout point costs MORE than a
                # scan point -- its points are farther apart, a full settle
                # each -- but this window knows only the dwell.)
                n_scout = est["points"] * est["blocks"]
                if n_scout:
                    self.summary.setText(self.summary.text()
                                         + f"  ·  scout {n_scout:,} pts first")
                    how = (f"for the scout ({n_scout:,} pts @ {self.per_pt.value():g}s/pt), "
                           f"then decided by the scout (at most "
                           f"{_fmt_duration(eta)} for every point)")
                    eta = n_scout * self.per_pt.value()
                else:
                    how += ", at most -- the mask file decides how many are measured"
        # REPEATS (repeat.py) are in n already -- every repeat is measured. An
        # interval can make the scan longer than its dwell: never shorter than
        # (N-1) x interval per pass.
        from scan_core.repeat import average_index, min_seconds
        floor = min_seconds(comp)
        if floor > eta:
            eta = floor
            how += ", paced by the repeat interval"
        avg = average_index(comp.dims)
        if avg is not None:
            kept = n // max(1, comp.dims[avg].size)
            self.summary.setText(self.summary.text()
                                 + f"  (average of {comp.dims[avg].size} -> {kept:,} stored)")
        # CAMERA IMAGES (scan_core/framestore.py): a frame per point adds up
        # fast -- say how big the file will be BEFORE the run, uncompressed
        # (compression usually takes it to 30-70 %), and whether the frames
        # will be written to the file as they come instead of held in memory
        from scan_core import framestore as FS
        sizes = FS.estimate(recipe, self.registry)
        if sizes:
            known = [b for b in sizes.values() if b is not None]
            total_b = sum(known) if known else None
            text = f"  ·  images {FS.format_bytes(total_b)}"
            if any(b is not None and b > FS.INCREMENTAL_ABOVE_BYTES for b in known):
                text += " (written to the file as they come)"
            self.summary.setText(self.summary.text() + text)
        # The pre-run ETA counts only what this window can know -- the dwell
        # per point (or per fly row) -- not settling, not routines such as an
        # autofocus, which on the rig can be most of the time. So it says
        # "dwell only", and once the scan runs it is replaced by the MEASURED
        # remaining time (_show_detail).
        self._detail_parts = (
            f"dims: {', '.join(d.name for d in comp.dims)}   ·   ",
            f"ETA ≈ {int(eta // 60):d}m {int(eta % 60):02d}s {how} (dwell only)",
            ("   ·   zig-zag" if self.zigzag_box.isChecked() else "")
            + ("   ·   diagonal row change" if self.diagonal_box.isChecked() else "")
            + conditions)
        self._show_detail()

    def _show_detail(self):
        """The detail line, with the ETA clause that fits NOW: while a scan
        runs, its measured remaining time; otherwise the dwell-only estimate."""
        if self._detail_parts is None:
            return
        head, pre, tail = self._detail_parts
        clause = pre
        if self._running and self.run_progress is not None:
            clause = f"~{_fmt_duration(self.run_progress[2])} left (measured)"
        note = getattr(self, "_scout_note", "") if self._running else ""
        self._detail_shown = head + clause + tail + (f"   ·   {note}" if note else "")
        self.detail.setText(self._detail_shown)

    def _scout_axes(self) -> dict:
        """{dim name: coarse step} of the rows ticked 'scout', X before Y:
        the INNER axis first (a raster: its x, then its y). A picture's
        columns are the first scouted axis, so two linear rows Y (outer) and
        X (inner) give {X, Y} -- the old mask's default."""
        out = {}
        for row in reversed(self.rows):
            if row.is_scout():
                for name in row.scout_dims():
                    out[name] = int(row.scout_step.value())
        return out

    def _sync_scout_section(self) -> None:
        """Give the SCOUT PASS section the ticked axes and the outer ones."""
        if not hasattr(self, "scout_section"):
            return
        axes = self._scout_axes()
        names = self._dim_names()
        ks = [names.index(n) for n in axes if n in names]
        outer = names[:min(ks)] if ks else []
        fly = any(r.is_fly() for r in self.rows)
        margins = {}
        for row in self.rows:
            if row.is_scout():
                margins.update(row.scout_margins())
        self.scout_section.set_axes(axes, outer, fly,
                                    {n: margins.get(n, "auto") for n in axes})

    def _sync_window_card(self) -> None:
        """Offer the resonance window for the TICKED detectors that support it."""
        if not hasattr(self, "window_card"):
            return
        ids = [it.data(0, QtCore.Qt.UserRole) for it in self._det_items()
               if it.checkState(0) == QtCore.Qt.Checked]
        ok = [pid for pid in ids
              if getattr(self.registry.get(pid), "window", None)]
        self.window_card.set_detectors(ok)

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
        if not self.rows or (errs and not self._server_judges()):
            self._rebuild_summary(); return
        if self.server is not None and not block:
            # the run pane shows a SCAN SERVER: the scan runs THERE
            self._server_submit(recipe=recipe)
            return
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
                ds = run(recipe, self.registry, created_iso="live", on_log=self._on_log,
                         on_window=self.window_card.show_state,
                         on_scout=self._on_scout,
                         attrs=self.run_info.attrs())
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
        # every scan starts un-paused -- also the next one of a queue: Pause
        # holds the CURRENT scan only
        self._show_pause_state(False, enabled=True)
        path = self.autosave_path(recipe)
        self.worker = ScanWorker(recipe, self.registry, save_path=path,
                                 attrs=self.run_info.attrs())
        self._run_started()
        self.worker.where.connect(self._on_where)
        self.worker.progress.connect(self._on_progress)
        self.worker.partial.connect(self._on_partial)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.saved.connect(self._on_saved)
        self.worker.save_failed.connect(self._on_save_failed)
        self.worker.log.connect(self._on_log)
        self.worker.paused.connect(self._on_paused)
        self.worker.ask.connect(self._on_ask)
        self.worker.window.connect(self.window_card.show_state)
        self.worker.scout.connect(self._on_scout)
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
        if bad and self._server_judges():
            bad = []                 # the scan server checks it against ITS registry
        if not entries or bad:
            self.detail.setText("queue NOT started -- " + (" | ".join(bad) or "it is empty"))
            return False
        if self.server is not None:
            # the whole queue goes to the scan server, which runs it there
            return self._server_submit(entries=entries)
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
        if self.server is not None:
            self._server_cmd("stop queue", self.server.stop_queue)
            return
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
        elif getattr(worker, "stop_all", False):
            # "Abort all": this scan stopped AND the queue ends here
            self._queue_stop = self._queue_stop or f"abort all: {worker.stop_reason}"
            self._qlog(f"queue: scan {self._queue_i + 1} '{entry.name}' {outcome} "
                       f"-- ABORT ALL, queue stopped")
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

    # ---- a SCAN SERVER (scan_core/scan_server.py) ------------------------------
    @property
    def server_submit(self) -> bool:
        """Run (and a loaded queue, and the queue edits) go to the attached
        scan server. Setting it re-shows what only matters when submitting."""
        return self._may_submit_flag

    @server_submit.setter
    def server_submit(self, on: bool) -> None:
        on = bool(on)
        if on != self._may_submit_flag:
            self._may_submit_flag = on
            if self.server is not None:
                self._sync_server_mode()

    def attach_server(self, watch, can_submit: bool = False, local: bool | None = None) -> None:
        """Show the scan of a scan server (apps/scan_server_view.ServerWatch)
        in this run pane. `can_submit`: Run (and a loaded queue) go to it.
        `local`: the server runs on this PC (default: ask the watch)."""
        from apps.scan_server_view import ServerFaults
        if self.server is watch:
            # the same server again (a setting changed): only the mode moves --
            # connecting the signals twice would draw everything twice
            self._server_local = bool(watch.is_local() if local is None else local)
            self.server_submit = can_submit
            self._sync_server_mode()
            return
        self.server = watch
        self._may_submit_flag = bool(can_submit)
        self._server_local = bool(watch.is_local() if local is None else local)
        self._server_faults = ServerFaults(watch)
        self._server_answered = ("", 0.0)
        self.abort_btn.setEnabled(False)
        self._show_pause_state(False, enabled=False)
        self.save_lbl.setText(f"watching the scan server on {watch.label}")
        # phase 2 of watching: the definitions and the lab's view
        self._server_scan = dict(getattr(watch, "scan", {}) or {})
        self._server_tree_key = None
        watch.scan_info.connect(self._on_server_scan)
        watch.view.connect(self._on_server_view)
        self._sync_server_mode()
        if not self._server_local and self.follow_view_box.isChecked() \
                and getattr(watch, "last_view", None):
            self._on_server_view(watch.last_view)

    def _sync_server_mode(self) -> None:
        """What the pane shows for the attached server, from two facts: is it
        THIS PC's server, and may this suite submit to it (server_submit).

        (d) of phase 2: this PC's scan name, RUN INFO and per-point time only
        matter for a scan started HERE -- next to a scan of the lab they
        would read as its name and sample (the lab's own are in the ON THE
        SCAN SERVER card). So they are shown exactly while this suite may
        submit: on the lab PC with "Run scans on this PC's scan server", on
        another PC while it holds control of the server (or nobody does)."""
        if self.server is None:
            return
        submit, local = self.server_submit, self._server_local
        busy = bool((self.server.last or {}).get("busy"))
        self.run_btn.setEnabled(submit and not busy)
        if submit and local:
            tip = ("Run this scan ON THE SCAN SERVER of this PC: it keeps running when\n"
                   "this window closes, and any PC can watch it.")
        elif submit:
            tip = ("Run this scan ON THE SCAN SERVER you are watching: it runs on that\n"
                   "PC with its instruments, is saved there (Copy to this PC fetches\n"
                   "the file), and carries this PC's run info. The server checks it\n"
                   "against its own instruments and limits.")
        elif local:
            tip = ("This pane is watching this PC's scan server. Tick 'Run scans on\n"
                   "this PC's scan server' (Settings tab) to start scans there.")
        else:
            tip = ("This pane is watching a scan server on another PC, and another PC\n"
                   "holds control of it. 'Take control' (above) to start scans here.")
        self.run_btn.setToolTip(tip)
        # the queue card mirrors what the server runs: not needed on the lab
        # PC's own suite while it is the one submitting (it IS the lab)
        self.server_box.setVisible(not (local and submit))
        for w in (self.name_lbl, self.name_edit, self.run_info, self.per_pt_lbl, self.per_pt):
            w.setVisible(submit)
        self._fill_server_tree()
        self._sync_queue_buttons()
        if local and submit:
            # the suite ON the server's PC tells watchers what it shows
            if not self._view_published:
                self.view.view_changed.connect(self._publish_view)
                self._view_published = True
            self._publish_view()
        elif self._view_published:
            self.view.view_changed.disconnect(self._publish_view)
            self._view_published = False

    def detach_server(self) -> None:
        """Back to running scans in this window."""
        if self._view_published:
            self.view.view_changed.disconnect(self._publish_view)
            self._view_published = False
        if self.server is not None:
            for sig, slot in ((self.server.scan_info, self._on_server_scan),
                              (self.server.view, self._on_server_view)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.server_box.hide()
        for w in (self.name_lbl, self.name_edit, self.run_info, self.per_pt_lbl, self.per_pt):
            w.show()
        self._server_scan = {}
        self.server_tree.clear()
        self.server = None
        self._may_submit_flag = False
        self._server_local = False
        self._server_faults = None
        self._on_paused([])
        self._on_ask("", None)
        self.run_btn.setToolTip("")
        self.run_btn.setEnabled(self.worker is None)
        self.abort_btn.setEnabled(self.worker is not None)
        self._show_pause_state(bool(self.worker is not None and self.worker._pause),
                               enabled=self.worker is not None)
        self.stop_queue_btn.setVisible(self.queue_running())
        self.queue_lbl.setVisible(self.queue_running())
        self.progress.setFormat("%p%")
        self.view.set_marker(None)
        self._server_was_busy = False
        self._show_where()
        self._refresh_save_target()
        self._rebuild_summary()

    def _server_cmd(self, what: str, fn, *args) -> bool:
        ok, why = fn(*args)
        msg = f"scan server: {what} sent" if ok else f"scan server: {what} REFUSED -- {why}"
        self.run_log.append(msg)
        if self.on_log is not None:
            self.on_log(msg)
        if not ok:
            self.detail.setText(msg)
        return ok

    def _server_submit(self, recipe=None, entries=None) -> bool:
        """Run on the server: this PC's (with the setting on), or -- phase 2 --
        a watched server on another PC while this suite may submit (control).
        The run info is THIS PC's Run info card; the server validates the
        scan against ITS instruments and saves the file on ITS PC."""
        if not self.server_submit:
            self.detail.setText(
                "Run is not available here: on the scan server's PC tick 'Run scans "
                "on this PC's scan server' (Settings tab); on another PC take control "
                "of the server first (another PC holds it). Stop watching to run "
                "in this window.")
            return False
        attrs = self.run_info.attrs()
        st = self.server.last or {}
        if st.get("busy"):
            # a queue (Load scan... with several files) while the server runs:
            # it goes to the END of the running queue (phase 2) instead of
            # being refused -- unless that queue is already stopping
            if entries is not None and not (st.get("queue") or {}).get("stop_reason"):
                return self._server_cmd(f"add {len(entries)} scans to the running queue",
                                        self.server.queue_add, entries, None, attrs)
            self.detail.setText("the scan server is already running a scan -- wait for it, "
                                "Abort it, or add yours with '+ Add to queue'")
            return False
        if entries is not None:
            ok = self._server_cmd(f"queue of {len(entries)} scans",
                                  self.server.submit_queue, entries, attrs)
        else:
            ok = self._server_cmd(f"scan '{recipe.name}'", self.server.submit, recipe, attrs)
        if ok:
            self.progress.setValue(0)
            self.run_btn.setEnabled(False)
            self.abort_btn.setEnabled(True)
            self._show_pause_state(False, enabled=True)
        return ok

    def _server_judges(self) -> bool:
        """True when a scan goes to a scan server on ANOTHER PC. Its own
        registry -- the instruments it drives, with their live limits -- is
        the one that counts: this window's copy (the mirrored instruments) is
        the same services under the same names, but may lag a module that
        just (re)started there, so the server's verdict decides, and its
        refusal is shown here word for word."""
        return self.server is not None and self.server_submit and not self._server_local

    # ---- editing the server's RUNNING queue (phase 2) -----------------------
    def _queue_entry(self, i):
        entries = (self._server_scan or {}).get("entries") or []
        return entries[i] if i is not None and 0 <= i < len(entries) else None

    def _waiting(self, i) -> bool:
        """The i-th scan of the server's queue has not started (editable)."""
        st = (self.server.last or {}) if self.server is not None else {}
        e = self._queue_entry(i)
        if e is None or not st.get("busy") or e.get("result"):
            return False
        pos = int((st.get("queue") or {}).get("pos") or 0) - 1
        return i > pos

    def _sync_queue_buttons(self) -> None:
        if not hasattr(self, "queue_add_btn"):
            return
        st = (self.server.last or {}) if self.server is not None else {}
        busy = bool(st.get("busy"))
        stopping = bool((st.get("queue") or {}).get("stop_reason"))
        may = self.server is not None and self.server_submit
        i = self._selected_server_entry(strict=True)
        n = len((self._server_scan or {}).get("entries") or [])
        waiting = may and self._waiting(i)
        self.queue_add_btn.setEnabled(may and busy and not stopping)
        self.queue_remove_btn.setEnabled(waiting)
        self.queue_up_btn.setEnabled(waiting and self._waiting(i - 1))
        self.queue_down_btn.setEnabled(waiting and i is not None and i + 1 < n)
        self.fetch_btn.setEnabled(self.on_fetch_file is not None
                                  and bool(self._fetch_target()))
        self.copy_def_btn.setEnabled(self._selected_server_entry() is not None)
        for b in (self.queue_add_btn, self.queue_remove_btn, self.queue_up_btn,
                  self.queue_down_btn):
            b.setVisible(may)

    def _queue_add_clicked(self) -> bool:
        """This Scan tab's definition, at the end of the server's running queue."""
        if self.server is None or not self.server_submit:
            return False
        self.refresh_axis_limits()
        recipe = self.build_recipe()
        if not self.rows:
            self.detail.setText("nothing to add: the axis stack on the Scan tab is empty")
            return False
        return self._server_cmd(f"add '{recipe.name}' to the queue", self.server.queue_add,
                                recipe, None, self.run_info.attrs())

    def _queue_remove_clicked(self) -> bool:
        i = self._selected_server_entry(strict=True)
        e = self._queue_entry(i)
        if e is None or self.server is None:
            return False
        return self._server_cmd(f"remove '{e.get('name')}' from the queue",
                                self.server.queue_remove, e.get("id"))

    def _queue_move_clicked(self, step: int) -> bool:
        i = self._selected_server_entry(strict=True)
        e = self._queue_entry(i)
        if e is None or self.server is None:
            return False
        self._keep_selected_id = e.get("id")      # the moved scan stays selected
        return self._server_cmd(f"move '{e.get('name')}' {'up' if step < 0 else 'down'}",
                                self.server.queue_move, e.get("id"), i + step)

    def _fetch_target(self):
        """(relative path on the server, name) of the file Copy to this PC
        takes: the selected finished scan, else the last one saved."""
        entries = (self._server_scan or {}).get("entries") or []
        i = self._selected_server_entry(strict=True)
        if i is not None and 0 <= i < len(entries):
            e = entries[i]
            return (e.get("rel_path"), e.get("name")) if e.get("rel_path") else None
        for e in reversed(entries):
            if e.get("rel_path"):
                return e["rel_path"], e.get("name")
        return None

    def _fetch_clicked(self):
        target = self._fetch_target()
        if not target or self.on_fetch_file is None:
            return None
        rel, name = target
        msg = f"copying '{name}' from the scan server's PC ..."
        self.detail.setText(msg)
        self.run_log.append(msg)
        return self.on_fetch_file(rel)

    def _server_answer(self, value) -> None:
        """The operator banner's buttons, for a question the SERVER asks."""
        self._server_answered = (self.ask_lbl.text(), time.monotonic())
        self._server_cmd("Continue" if value is True else
                         "Abort ALL" if value == "all" else "Abort scan",
                         self.server.answer, value)

    def show_server_status(self, st: dict) -> None:
        """Draw the server's status in this pane: progress, banners, buttons,
        the queue line, where the file goes. Called at every status frame."""
        if self.server is None or not isinstance(st, dict):
            return
        busy = bool(st.get("busy"))
        done, total = int(st.get("done") or 0), int(st.get("total") or 0)
        if total:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
        # PAUSED on a fault (the server knows which can be cleared)
        faults = [(f.get("module", "?"), f.get("message", "")) for f in st.get("faults") or []]
        if faults != list(self.paused_faults):
            self._on_paused(faults)
        # YOUR TURN: a pause step on the server waits for an answer
        msg = st.get("pause_message") or ""
        answered, t = self._server_answered
        if msg and not (msg == answered and time.monotonic() - t < 3.0):
            if self._ask_answer is None or self.ask_lbl.text() != msg:
                self._on_ask(msg, self._server_answer)
        elif not msg and self.ask_box.isVisible():
            self._on_ask("", None)
        # the queue
        q = st.get("queue") or {}
        n, pos = int(q.get("n") or 0), int(q.get("pos") or 0)
        if busy and n > 1:
            left = st.get("queue_eta_s")
            from scan_core.scan_server import fmt_duration
            self.queue_lbl.setText(f"Scan {pos} of {n}  ·  {st.get('scan', '')}"
                                   + (f"   ·   queue ≈ {fmt_duration(left)} left"
                                      if left is not None else ""))
            self.queue_lbl.show()
        elif not busy and q.get("summary"):
            self.queue_lbl.setText(q["summary"])
            self.queue_lbl.show()
        else:
            self.queue_lbl.hide()
        self.stop_queue_btn.setVisible(busy and n > 1)
        self.stop_queue_btn.setEnabled(busy)
        # where the data goes: a path ON THE SERVER'S PC (no file transfer)
        err = st.get("save_error") or ""
        path = st.get("last_saved") or st.get("save_path") or ""
        pc = st.get("pc") or "?"
        if err:
            self.save_lbl.setText(err)
            self.save_lbl.setStyleSheet(f"color:{C['danger']}; font-size:11px;")
        else:
            self.save_lbl.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
            self.save_lbl.setText((f"the server saves to {path} (on {pc})" if path else
                                   f"data folder on {pc}: {st.get('data_dir', '?')}"))
        if path:
            self.last_saved = Path(path)
        # buttons: Abort while anything runs; Run only when we may submit
        self.abort_btn.setEnabled(busy)
        self.run_btn.setEnabled(self.server_submit and not busy)
        self._sync_queue_buttons()
        # Pause follows the SERVER's flag (whoever pressed it). Pause is a
        # safety verb like Abort (enabled while busy, for every PC); Resume
        # needs control, so it is only offered when the server would take it
        user_paused = busy and bool(st.get("user_paused"))
        # A click shows its result at once; a status frame the server sent
        # BEFORE it took the click must not flip the button back (Lukas
        # 2026-10-07: "Resume shows pause, then resume, then pause again").
        # Keep the clicked state until the server agrees, at most HOLD_S.
        want = self._pause_click
        if want is not None:
            if not busy or user_paused == want[0] or time.monotonic() > want[1]:
                self._pause_click = None
            else:
                user_paused = want[0]
        self._show_pause_state(user_paused,
                               enabled=busy and (not user_paused or self._server_may_resume()))
        if busy:
            self.summary.setStyleSheet("")
            self.summary.setText(f"on the scan server:  {st.get('scan', '')}")
        elif getattr(self, "_server_was_busy", False):
            # the server's scan ended: back to this tab's own definition
            self._rebuild_summary()
            if st.get("error"):
                self.detail.setText(f"scan server: {st['error']}")
        self._server_was_busy = busy
        # where it is, marked on the live map; the suite's header line
        where = st.get("where_axes") if busy else None
        self.run_where = where
        coords = {a["name"]: a["value"] for a in (where or {}).get("axes", ())
                  if a.get("value") is not None}
        self.view.set_marker(coords or None)
        if self.on_status is not None:
            self.on_status(st.get("where") or "")
        self._fill_server_tree(st)

    # ---- phase 2 of watching: definitions, view ------------------------------
    def _publish_view(self):
        """What this PC shows -- the plot, plus whatever the host adds
        (`view_extra`: the suite adds its Control tab's panel) -- to the
        scan server, for the PCs watching it."""
        if self.server is None or not (self.server_submit and self._server_local):
            return
        view = {"plot": self.view.view_state() if self.view.ds is not None else None}
        if self.view_extra is not None:
            view.update(self.view_extra() or {})
        self.server.set_view(view)

    def _on_server_view(self, reply):
        # the lab's own suite publishes, never follows (it IS the lab); a
        # watcher on another PC follows even while it may submit (phase 2)
        if self.server is None or (self.server_submit and self._server_local) \
                or not self.follow_view_box.isChecked():
            return
        view = (reply or {}).get("view") if isinstance(reply, dict) else None
        if not view:
            return
        plot = view.get("plot") if "plot" in view else view   # b430e5e sent it flat
        if plot:
            self.view.apply_view_state(plot)
        if self.on_server_view is not None:
            self.on_server_view(view)

    def _follow_view_toggled(self, on: bool):
        if on and self.server is not None:
            self._on_server_view(getattr(self.server, "last_view", None))

    def _on_server_scan(self, reply):
        self._server_scan = dict(reply or {})
        self._server_tree_key = None
        self._fill_server_tree()

    def _fill_server_tree(self, st: dict | None = None):
        """The queue (one line per scan, marked done / running / aborted /
        waiting) with each scan's definition under it. Rebuilt only when the
        queue or a result changes, so an expanded entry stays expanded."""
        from apps.scan_server_view import definition_lines
        if self.server is None:
            return
        st = st if st is not None else (self.server.last or {})
        entries = self._server_scan.get("entries") or []
        q = st.get("queue") or {}
        results = {i: r for i, r in enumerate(q.get("results") or [])}
        busy = bool(st.get("busy"))
        pos = int(q.get("pos") or 0) - 1 if busy else -1
        key = (self._server_scan.get("scan_rev"), self._server_scan.get("queue_rev"),
               pos, busy, tuple(tuple(r) for r in (q.get("results") or [])))
        if key == self._server_tree_key:
            return
        self._server_tree_key = key
        # what was selected and opened is remembered by the scan's ID: a queue
        # edit moves scans, and the scan that was open must stay open
        ID = QtCore.Qt.UserRole + 1
        keep = getattr(self, "_keep_selected_id", None)
        self._keep_selected_id = None
        cur = self.server_tree.currentItem()
        if keep is None and cur is not None:
            top_cur = cur.parent() or cur
            keep = top_cur.data(0, ID)
        expanded = {self.server_tree.topLevelItem(k).data(0, ID)
                    for k in range(self.server_tree.topLevelItemCount())
                    if self.server_tree.topLevelItem(k).isExpanded()}
        if self._server_scan.get("scan_rev") != getattr(self, "_server_tree_rev", None):
            expanded = set()                 # a new submission: start closed
        self._server_tree_rev = self._server_scan.get("scan_rev")
        self.server_tree.clear()
        if not entries:
            QtWidgets.QTreeWidgetItem(self.server_tree, ["nothing submitted yet"])
            self.copy_def_btn.setEnabled(False)
            self._size_server_tree()
            return
        by = self._server_scan.get("started_by") or ""
        for i, e in enumerate(entries):
            res = results.get(i)
            mark = ("running" if i == pos else
                    (e.get("result") or (res[1] if res else
                                         ("waiting" if busy and i > pos else ""))))
            n = f"{e.get('n_points')} pts" if e.get("n_points") else ""
            added = f"added by {e['added_by']}" if e.get("added_by") else ""
            label = "  ·  ".join(x for x in (f"{i + 1}. {e.get('name', 'scan')}", mark, n,
                                             added) if x)
            top = QtWidgets.QTreeWidgetItem(self.server_tree, [label])
            top.setData(0, QtCore.Qt.UserRole, i)
            top.setData(0, ID, e.get("id", i))
            if i == pos:
                top.setForeground(0, QtGui.QBrush(QtGui.QColor(C["accent"])))
            section = None
            for sec, text in definition_lines(e.get("recipe") or {}, e.get("attrs") or {}):
                if sec != section:
                    section = sec
                    head = QtWidgets.QTreeWidgetItem(top, [sec.upper()])
                    head.setForeground(0, QtGui.QBrush(QtGui.QColor(C["muted"])))
                    head.setData(0, QtCore.Qt.UserRole, i)
                it = QtWidgets.QTreeWidgetItem(top, ["    " + text])
                it.setData(0, QtCore.Qt.UserRole, i)
            if by and i == 0:
                it = QtWidgets.QTreeWidgetItem(top, [f"started by {by}"])
                it.setData(0, QtCore.Qt.UserRole, i)
            if e.get("rel_path"):
                it = QtWidgets.QTreeWidgetItem(top, [f"    file on the server: {e['rel_path']}"])
                it.setData(0, QtCore.Qt.UserRole, i)
            if e.get("id", i) in expanded:
                top.setExpanded(True)       # what the operator opened stays open
            if keep is not None and keep == e.get("id", i):
                self.server_tree.setCurrentItem(top)
        self._sync_queue_buttons()
        self._size_server_tree()

    def _size_server_tree(self):
        """As tall as the lines that are open, at most SERVER_TREE_MAX px: the
        queue is a line per scan, and the live plot below keeps its room
        until somebody opens a definition to read it."""
        tree = self.server_tree
        rows, it = 0, tree.topLevelItem(0)
        while it is not None:
            rows += 1
            it = tree.itemBelow(it)
        h = tree.sizeHintForRow(0) if tree.topLevelItemCount() else 18
        tree.setFixedHeight(min(SERVER_TREE_MAX, max(1, rows) * max(h, 16) + 6))

    def _selected_server_entry(self, strict: bool = False) -> int | None:
        """The index of the selected scan of the server's queue. Not
        `strict`: with nothing selected, a queue of one counts as selected
        (Copy to Scan tab); the queue edits want an explicit choice."""
        it = self.server_tree.currentItem()
        if it is None:
            if strict:
                return None
            entries = (self._server_scan or {}).get("entries") or []
            return 0 if len(entries) == 1 else None
        i = it.data(0, QtCore.Qt.UserRole)
        return int(i) if isinstance(i, int) else None

    def definition_state(self) -> dict | None:
        """The Scan tab's definition as a plain recipe dict, or None when it
        cannot be built (an empty stack is still a definition)."""
        try:
            return json.loads(self.build_recipe().to_json())
        except Exception:
            return None

    def apply_definition(self, d: dict) -> list[str]:
        """Show another PC's Scan tab definition here (a watched lab's)."""
        if not isinstance(d, dict) or d == self.definition_state():
            return []
        self._applying_definition = True
        try:
            return self.load_recipe(Recipe.from_dict(d))
        except Exception:
            return []
        finally:
            self._applying_definition = False

    def _copy_server_definition(self) -> list[str] | None:
        """Load the selected server scan's definition into this Scan tab."""
        i = self._selected_server_entry()
        entries = (self._server_scan or {}).get("entries") or []
        if i is None or not 0 <= i < len(entries):
            return None
        e = entries[i]
        try:
            recipe = Recipe.from_dict(e.get("recipe") or {})
        except Exception as exc:
            self.detail.setText(f"could not read that definition: {exc}")
            return None
        recipe.name = e.get("name") or recipe.name
        missing = self.load_recipe(recipe)
        msg = (f"copied '{recipe.name}' from the scan server"
               + (f" -- not available on this PC: {', '.join(missing)}" if missing else ""))
        self.detail.setText(msg)
        self.run_log.append(msg)
        if self.on_log is not None:
            self.on_log(msg)
        return missing

    def show_server_dataset(self, ds) -> None:
        """A live (or the final) dataset from the server."""
        self._on_partial(ds)

    def autosave_path(self, recipe) -> Path | None:
        """One file per run: <data dir>/<date>/<time>_<name>.nc.

        Dated folders because a day's scans belong together, and the time in
        the name because a scan is normally repeated with one thing changed --
        overwriting the previous one is how an afternoon's work disappears.
        """
        if not self.autosave_dir:
            return None
        # The rule lives in scan_core/autosave.py, shared with the scripting
        # API (scan_core/api.py), so a script saves exactly like this tab.
        return autosave.autosave_path(self.autosave_dir, recipe.name)

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
        # The probe (write a file where the data will go, no mkdir) is shared
        # with the scripting API: scan_core/autosave.py.
        ok, msg = autosave.probe_save_target(self.autosave_dir)
        if not ok:
            return False, msg
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

    def _on_save_failed(self, message: str):
        """The FILE could not be written (full disk, share gone). The scan goes
        on -- so this must NOT end the run in the UI the way `failed` does:
        say it in red where the file name is shown, and in the log."""
        self.save_lbl.setText(message)
        self.save_lbl.setStyleSheet(f"color:{C['danger']}; font-size:11px;")
        self.run_log.append(message)
        if self.on_log is not None:
            self.on_log(message)

    def _on_paused(self, faults):
        """Show (faults) or hide ([]) the PAUSED banner."""
        self.paused_faults = list(faults or [])
        # the old buttons go; the modules at fault now get theirs
        for b in self.clear_fault_btns.values():
            self.pause_btns.removeWidget(b)
            b.setParent(None)
        self.clear_fault_btns = {}
        if not self.paused_faults:
            self.pause_box.hide()
            self.progress.setFormat("PAUSED -- %p%" if self._user_paused else "%p%")
            return
        self.pause_lbl.setText("\n".join(f"{name}: {msg}" for name, msg in self.paused_faults))
        self.progress.setFormat("PAUSED -- %p%")
        # a scan on a SCAN SERVER: the server knows which faults can be
        # cleared, and the button sends clear_fault through it
        lab = self._server_faults if self.server is not None else self.fault_lab
        for name, _ in self.paused_faults:
            if name in self.clear_fault_btns or lab is None:
                continue
            try:
                ok = lab.can_clear_fault(name)
            except Exception:
                ok = False
            if not ok:
                continue
            b = QtWidgets.QPushButton(f"Clear fault on {name}")
            b.setToolTip(f"Send `clear_fault` to {name}. Do it once the cause is\n"
                         f"fixed (the sample found again, the pattern re-taught):\n"
                         f"the module latches its fault until someone has looked.")
            b.clicked.connect(lambda _=False, n=name: self.clear_fault(n))
            self.pause_btns.insertWidget(self.pause_btns.count() - 1, b)
            self.clear_fault_btns[name] = b
        self.pause_box.show()

    def _on_ask(self, message: str, answer):
        """Show (answer given) or hide (answer None) the operator banner."""
        self._ask_answer = answer
        if answer is None:
            self.ask_box.hide()
            return
        self.ask_lbl.setText(message or "(no message)")
        self.ask_box.show()
        self.ask_continue_btn.setFocus()
        QtWidgets.QApplication.alert(self.window())   # flash the taskbar button

    def answer_pause(self, go_on) -> bool:
        """The banner's buttons: Continue (True) or Abort scan (False).
        False if no question is open."""
        answer, self._ask_answer = self._ask_answer, None
        self.ask_box.hide()
        if answer is None:
            return False
        # True = Continue, False = Abort scan, "all" = Abort all (the queue too)
        answer(go_on if go_on == "all" else bool(go_on))
        msg = ("operator: Abort ALL (at the pause)" if go_on == "all" else
               "operator: Continue" if go_on else "operator: Abort scan (at the pause)")
        self.run_log.append(msg)
        return True

    def clear_fault(self, name: str) -> bool:
        """Send `clear_fault` to one module (the banner's button)."""
        msg = f"clear_fault sent to {name}"
        ok = True
        lab = self._server_faults if self.server is not None else self.fault_lab
        try:
            lab.clear_fault(name)
        except Exception as exc:
            # a module refuses while the cause is still there -- say so
            msg, ok = f"clear_fault on {name} refused: {exc}", False
        self.run_log.append(msg)
        self.pause_hint.setText(msg + ". The scan resumes when every fault is gone.")
        if self.on_log is not None:
            self.on_log(msg)
        return ok

    def _on_progress(self, done, total, eta):
        # the points have started; the point that was in progress when Pause
        # was pressed still reports -- keep saying PAUSED then
        self.progress.setFormat("PAUSED -- %p%" if self._user_paused else "%p%")
        self.progress.setMaximum(total); self.progress.setValue(done)
        self.run_progress = (int(done), int(total), float(eta))
        if self.queue_running():
            self._queue_label(eta)
        else:
            self._show_detail()             # the ETA becomes the measured one
        self._show_where()

    # ---- where the running scan is -------------------------------------------
    def _run_started(self):
        """A scan is starting: forget the last one's position."""
        self._running = True
        self.run_where, self.run_progress, self.run_now = None, None, ""
        self._scout_note, self._scout_var, self._scout_prev = "", None, ""
        self.view.set_marker(None)
        self._show_where()

    def _on_where(self, where):
        """The engine's where_of() of the point just measured: kept for the
        status line, and marked on the live plot."""
        self.run_where = where
        coords = {a["name"]: a["value"] for a in (where or {}).get("axes", ())
                  if a.get("value") is not None}
        self.view.set_marker(coords)

    def run_status_text(self) -> str:
        """One line: WHERE the running scan is ("" when idle). E.g.

            scan 2 of 3   point 25 / 125   dssg.frequency 1000 MHz (1/5)
            camera.scan_ix 24 (25/25)   ~56m 00s left   now: run camera.autofocus
            (start of each sweep of camera.scan_ix)

        Every number comes from the engine (index, values, measured remaining
        time); nothing here recomputes the order of the points.
        """
        if not self._running:
            return ""
        bits = []
        if self._user_paused:
            # first, so a glance at the header line says it
            bits.append("PAUSED (Resume carries on)")
        if self.queue_running() and self._queue_i >= 0:
            bits.append(f"scan {self._queue_i + 1} of {len(self._queue)}")
        prog = self.run_progress
        bits.append(f"point {prog[0]:,} / {prog[1]:,}" if prog else "starting")
        where = self.run_where or {}
        if where.get("row"):
            bits.append("row {} / {}".format(*where["row"]))
        for a in where.get("axes", ()):
            if a.get("value") is None:
                continue                    # a fly axis: the whole row at once
            unit = f" {a['unit']}" if a.get("unit") else ""
            bits.append(f"{a['name']} {a['value']:g}{unit} ({a['i'] + 1}/{a['n']})")
        if prog and prog[0] < prog[1]:
            bits.append(f"~{_fmt_duration(prog[2])} left")
        if self.run_now:
            bits.append(f"now: {self.run_now}")
        return "   ".join(bits)

    #: Set by the suite: called with run_status_text() whenever it changes,
    #: so the Measurement tab's header follows every point, not its 0.5 s clock.
    on_status = None

    def _show_where(self):
        text = self.run_status_text()
        self.where_lbl.setText(text)
        self.where_lbl.setVisible(bool(text) and not self.embedded)
        if self.on_status is not None:
            self.on_status(text)

    @staticmethod
    def _routine_step(msg: str) -> str | None:
        """'<when>: <step> ...' (a routine step starting) -> '<step> (<when>)';
        '' for the step's end (done / FAILED / not waited for); None for any
        other message. The format is hooks.py's `call` (step())."""
        label, sep, what = msg.partition(": ")
        if not sep:
            return None
        if what.endswith(" ..."):
            return f"{what[:-4]} ({label})"
        if what.endswith((" done", "carrying on", "(aborted)")):
            return ""
        return None

    def _on_log(self, msg: str):
        """A routine step. Shown IN the progress bar -- which otherwise sits at
        0 % through a two-minute magnet ramp and reference sweep, saying nothing
        about why -- as "now: ..." in the status line, and passed to the
        suite's log."""
        self.run_log.append(msg)
        self.progress.setFormat(msg)
        step = self._routine_step(msg)
        if step is not None:
            self.run_now = step
            self._show_where()
        if self.on_log is not None:
            self.on_log(msg)

    # not U+23F8 (the pause sign): the Windows UI font has no glyph for it
    # and it rendered as an empty box; block characters are there
    #: seconds a Pause / Resume click on a SERVER scan wins over status frames
    #: that still show the old state (they were sent before the click landed)
    PAUSE_CLICK_HOLD_S = 3.0
    PAUSE_TEXT = "▌▌ Pause"
    RESUME_TEXT = "▶ Resume"
    PAUSE_TIP = ("Hold the scan after the point being measured; Resume carries on. "
                 "Abort still works while paused.")

    def _show_pause_state(self, paused: bool, enabled: bool) -> None:
        """Draw the Pause button (and the PAUSED marks) for `paused`."""
        changed = bool(paused) != self._user_paused
        self._user_paused = bool(paused)
        self.pause_btn.setText(self.RESUME_TEXT if paused else self.PAUSE_TEXT)
        self.pause_btn.setEnabled(bool(enabled))
        if changed:
            fmt = self.progress.format()
            if paused and not fmt.startswith("PAUSED"):
                self.progress.setFormat("PAUSED -- %p%")
            elif not paused and fmt == "PAUSED -- %p%":
                self.progress.setFormat("%p%")
            self._show_where()

    def is_user_paused(self) -> bool:
        return self._user_paused

    def pause_scan(self) -> bool:
        """Pause the running scan (between points). False if none runs."""
        if self.server is not None and self.worker is None:
            # a SAFETY verb on the server, like abort: allowed from every PC
            if self._server_cmd("pause", self.server.pause):
                self._pause_click = (True, time.monotonic() + self.PAUSE_CLICK_HOLD_S)
                self._show_pause_state(True, enabled=self._server_may_resume())
                return True
            return False
        if self.worker is None:
            return False
        self.worker.pause()
        self.run_log.append("operator: Pause")
        if self.on_log is not None:
            self.on_log("operator: Pause -- the scan holds after the point being measured")
        self._show_pause_state(True, enabled=True)
        return True

    def resume_scan(self) -> bool:
        """Carry on with a paused scan. False if none runs (or refused)."""
        if self.server is not None and self.worker is None:
            # needs control on the server, like submit
            if self._server_cmd("resume", self.server.resume):
                self._pause_click = (False, time.monotonic() + self.PAUSE_CLICK_HOLD_S)
                self._show_pause_state(False, enabled=True)
                return True
            return False
        if self.worker is None:
            return False
        self.worker.resume()
        self.run_log.append("operator: Resume")
        if self.on_log is not None:
            self.on_log("operator: Resume")
        self._show_pause_state(False, enabled=True)
        return True

    def _toggle_pause(self):
        if self._user_paused:
            self.resume_scan()
        else:
            self.pause_scan()

    def _server_may_resume(self) -> bool:
        """Would the scan server accept `resume` from us? It needs control
        when somebody holds it -- so: nobody holds it, or we do."""
        fn = getattr(self.server, "control_text", None)
        if fn is None:
            return True
        try:
            return fn()[0] in ("you", "free")
        except Exception:
            return True

    def _abort(self):
        if self.server is not None and self.worker is None:
            # Abort is a SAFETY verb on the server: allowed from every PC
            self._server_cmd("abort", self.server.abort)
            return
        if self.worker:
            self.worker.abort()

    def stop_for_close(self, timeout_s: float = 30.0) -> bool:
        """The window is closing: ABORT a running scan (and its queue) and wait
        for it to end. Returns True if nothing is left running.

        Why wait: closing used to tear the instrument connections down under
        the running scan -- a ZeroMQ socket closed from the GUI thread while
        the scan thread was inside a request on it -- and let the process exit
        with the scan thread still going, so the after-scan routine ("field ->
        0") never ran. An Abort reaches every settle wait at once, and the
        after-scan routine after an Abort sends its commands without waiting
        for them, so this normally takes well under a second.
        """
        if self.queue_running():
            self._queue_stop = "the window was closed"
        w = self.worker
        if w is None or not w.isRunning():
            return True
        w.abort()
        return bool(w.wait(int(timeout_s * 1000)))

    def closeEvent(self, ev):
        self.stop_for_close()
        super().closeEvent(ev)

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
        var = getattr(self, "_scout_var", None)
        if var and var in ds.data_vars and self.view.det_combo.currentText() != var:
            # while the SCOUT runs its map is what is filling in: show it
            # (rig, 2026-10-08: the plot stayed empty through the whole scout)
            self.view.apply_view_state({"detector": var})

    def _on_scout(self, st):
        """The SCOUT PASS (scan_core/scout.py): its progress in the bar, its
        map in the live plot while it runs, and -- once the mask is made --
        the threshold it used and how many points will be measured, kept in
        the detail line for the rest of the run."""
        where = f" {st['where']}" if st.get("where") else ""
        if st.get("phase") == "scout":
            if getattr(self, "_scout_var", None) is None:
                self._scout_prev = self.view.det_combo.currentText()
                self._scout_var = st.get("variable")
            self.progress.setFormat(
                f"SCOUT{where}: {st['done']}/{st['total']} points  ·  "
                f"~{_fmt_duration(st.get('eta_s') or 0.0)} left in the scout")
            return
        if st.get("phase") != "made":
            return
        self._scout_var = None
        prev = getattr(self, "_scout_prev", "")
        if prev and self.view.det_combo.findText(prev) >= 0:
            self.view.apply_view_state({"detector": prev})
        unit = f" {st['unit']}" if st.get("unit") else ""
        how = ("deviation" if st.get("keep") == "deviates" else "threshold")
        n, of = int(st.get("kept", 0)), max(1, int(st.get("of", 1)))
        self._scout_note = (f"scout{where}: {how} {st.get('threshold', float('nan')):.4g}{unit} "
                            f"-> {n:,} of {of:,} points ({100.0 * n / of:.0f} %); "
                            f"{int(st.get('measured', 0)):,} of {int(st.get('total', 0)):,} "
                            f"in the scan")
        self.progress.setFormat("%p%")
        self._show_detail()

    def _fill_det_combo(self, ds):
        """Hand the dataset to the viewer (it keeps the operator's choices)."""
        self.view.set_dataset(ds)
        self.live_image.set_dataset(ds)

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
        self._show_pause_state(False, enabled=False)   # ... nor held by the operator
        self._on_paused([])                 # a finished run is never paused
        self._on_ask("", None)              # ... nor waiting for the operator
        self.progress.setFormat("%p%")
        # Between two scans of a queue Run stays off: the queue is still going.
        self.run_btn.setEnabled(not self.queue_running())
        self.abort_btn.setEnabled(False)
        self.worker = None          # is_aborting() is False again for the next run
        # No longer "here": drop the mark and the status line, and put the
        # dwell-only ETA back -- unless the line now says something else (why
        # the run failed), which must stay readable.
        was_running = self.detail.text() == self._detail_shown
        self._running = False
        self.view.set_marker(None)
        self._show_where()
        if was_running and not self.queue_running():
            self._show_detail()

    def _update_plot(self):
        """Redraw with whatever the viewer's controls currently say."""
        self.view.refresh()

    # ---- dialogs ----------------------------------------------------------
    def _recall_dialog(self, path=None):
        """Pick a measured .nc and open the recall dialog (apps/recall.py)."""
        from apps.recall import open_recall
        return open_recall(self, self.lab, path,
                           start_dir=str(self.autosave_dir or ""))

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
            # through write_dataset: a big camera map's frames live in the
            # scan's file, not in the dataset, and are copied along
            autosave.write_dataset(self.dataset, fn)


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
