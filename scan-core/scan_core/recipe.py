"""
recipe.py — a SCAN as data (the schema we lock first).

A "recipe" fully describes a measurement without any hardware knowledge:
what to sweep (axes), what to record (detectors), and what to do at each step
(hooks). It is pure data — a dict you can save as YAML, diff, version, share,
and re-run headless. The engine (engine.py) and the GUI both consume it; neither
hardcodes any parameter, which is what makes the system expandable: a new knob
is just a new id that can appear in an axis or detector.

Top-level shape
---------------
name:      str
comment:   str
output:    {dir, basename, format}
axes:      [Axis, ...]      # OUTER→INNER. axes[0] is the slowest (outermost) loop.
detectors: [param_id, ...]  # gettables recorded at every point
hooks:     [Hook, ...]       # optional actions bound to a level/cadence
diagonal:  bool              # at a row change send all setpoints, then wait
window:    {...}             # optional WINDOW (window.py): sweep a slow array
                             # detector only around the peak a predictor
                             # (predictors.py) expects from the point's inputs
scout:     {...}             # optional SCOUT PASS (scout.py): a quick look at
                             # every k-th point of the ticked axes first, then
                             # measure only where something is happening
                             # (the old `mask:` block of 2026-10-07 is read
                             # and translated; only `scout` is written)

A ROUTINE is a hook with action `call` (see hooks.py):
  {when: before_scan, action: call,
   args: {set: {field: 190}, action: vna_reference}}
  {when: after_scan,  action: call, args: {set: {field: 0}}}
and one with several steps in a given order (2026-09-25):
  {when: before_scan, action: call,
   args: {steps: [{action: sim_autofocus}, {set: {field: 190}},
                  {action: vna_reference}, {set: {field: 0}}]}}
Five more step kinds (2026-10-04): wait_until, abort_if, skip_if, pause,
comment, compute_set -- see hooks.py; their conditions and formulas use the
restricted evaluator in expr.py and are checked by validate().

Axis types (the `type` field discriminates)
-------------------------------------------
Every axis except a repeat may carry `zigzag: true` (2026-10-11, Lukas: "Zig
zag goes to the advanced settings of each axis"): that axis is swept
BACKWARDS on every other sweep, i.e. every other time the axes outside it
advance (dim_zigzag below; engine.visit_index). On a fly axis
it means every other row is flown back. On the outermost axis it does
nothing (nothing outside it advances) -- except on a grid, whose inner
(fast) dim then zig-zags. The OLD scan-wide `zigzag: true` (before
2026-10-11) is gone: a definition that still has it is translated into
per-axis flags when it is read (upgrade_scan_wide_zigzag) and is never
written that way again.

linear : {type, param, start, stop, num, [spacing, name]} -> 1 dim
          `spacing: log` (2026-10-12) puts the points at equal RATIOS
          (np.geomspace) instead of equal steps; from and to must then have
          the same sign and neither be 0. Absent = linear, as always.
          SEGMENTS (2026-10-14, Lukas: "scan up and down ... stacking scanning
          arrays"): instead of start/stop/num, `segments: [{start, stop, num,
          [spacing]}, ...]` -- several linear (or log) runs visited in order,
          e.g. 0 -> 100 -> -100 -> 100 mT for a hysteresis loop. Where a
          segment starts on the value the previous one ended on, that joint is
          NOT measured twice (the segment starts one step after it). Values may
          repeat (that is the point: up AND down). Each point's segment is
          recorded in the file (`<dim>_segment`, see segment_coords).
array  : {type, param, values:[...], [name]}              -> 1 dim
file   : {type, param, path, [name]}                      -> 1 dim (values from CSV/txt)
zip    : {type, name, members:[{param, ...spec}], }       -> 1 dim, several params in lockstep
grid   : {type, x:{param,start,stop,num}, y:{...}, fast}  -> 2 dims: a 2-D grid of ANY
          two knobs in one entry (an XY map, field x frequency, ...). `x` and
          `y` are only the names of its two members; `fast` (x or y) is the
          inner one. Until 2026-10-12 this was `raster`; a definition that
          still says so is read as `grid` (upgrade_axes) and never written
          that way again.
fly    : {type, param, start, stop, num, speed, [speed_param, readback,
          lag_correction, name]}                          -> 1 dim, INNERMOST only:
          one continuous move per row, binned by the measured position
          (flyscan.py). Same coordinates as a linear axis with that start/stop/num.
          Since 2026-10-09 `param` may be ANY knob whose module declares a
          `ramp` block (field, frequency, ...): the module sweeps it at
          `speed` (its unit/s) -- or at the pace that makes a row last
          `row_time_s`, or at its default rate -- and the samples are binned
          by the ramp's readback (measured, or commanded; ramp.py).
repeat : {type, num, [mode: keep|average, interval_s, name]} -> 1 dim that sets
          NOTHING: everything inside it is done `num` times (repeat.py). `keep`
          keeps every repeat as a dimension; `average` stores mean/_std/_n.

`compile(registry)` turns the axis list into an ordered list of Dim objects
(a grid expands to two dims), which the engine iterates as an odometer. Keeping
"how you describe a scan" (axes) separate from "how you run it" (dims) is what
lets a 2-D grid be one entry in the UI yet a genuine 2 dimensions in the data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import yaml


# ─────────────────────────────── compiled form ────────────────────────────────

@dataclass
class Dim:
    """One realized dimension of the N-D grid the engine sweeps.

    name   : dimension/coordinate name in the dataset (unique)
    params : list of (settable_id, values[]) advanced together for this dim.
             len==1 for a normal axis; >1 for a zip (lockstep) axis;
             EMPTY for a repeat axis, which sets nothing (repeat.py).
    values : the coordinate of a dim without params (a repeat: 0..N-1)
    mode, interval_s : a repeat's mode (keep/average) and interval
    """
    name: str
    params: list[tuple[str, np.ndarray]]
    size: int
    kind: str = "linear"
    values: np.ndarray | None = None
    mode: str = ""
    interval_s: float | None = None
    #: SEGMENTS (2026-10-14): which run of the axis each point belongs to
    #: (int, 0, 1, 2 ...) and one dict per run -- or None for an ordinary
    #: monotonic axis. Set for a `segments` axis, and for a numeric list
    #: whose values turn back (split at the reversals); see axis_segments.
    segment: np.ndarray | None = None
    segments: list | None = None
    segment_source: str = ""

    @property
    def coord(self) -> np.ndarray:
        if not self.params:               # a repeat: its own 0..N-1
            return self.values
        return self.params[0][1]          # primary member is the coordinate


@dataclass
class CompiledScan:
    dims: list[Dim]
    detectors: list[str]
    hooks: list[dict]

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(d.size for d in self.dims)

    @property
    def n_points(self) -> int:
        n = 1
        for d in self.dims:
            n *= d.size
        return n


# ─────────────────────────── sweep-spec → values[] ────────────────────────────

def _is_number(v) -> bool:
    return isinstance(v, (int, float, np.integer, np.floating, bool, np.bool_))


def native_values(vals) -> np.ndarray:
    """A list of setpoints as an array: float when every one is a number
    (bools count as 0/1, as they always have), else an OBJECT array of the
    values as they are -- the options of an enum knob ("AC", "DC"), text."""
    vals = list(vals)
    if all(_is_number(v) for v in vals):
        return np.asarray(vals, dtype=float)
    out = np.empty(len(vals), dtype=object)
    out[:] = vals
    return out


def is_text_values(vals) -> bool:
    """True for an object array of non-numeric setpoints (native_values)."""
    return getattr(vals, "dtype", None) is not None and vals.dtype == object


def _values_from_spec(spec: dict) -> np.ndarray:
    """Turn a per-axis/per-member spec into an explicit value vector.

    Accepts either {start, stop, num} (linear), {start, stop, step}, or
    {values:[...]} / {path:...}. One place so every axis type shares it.
    `values` and a file may hold NON-numeric setpoints (2026-10-12: an enum
    knob's options, text); they come back as an object array (native_values).
    Linear spacing is for numbers only.
    `segments` (2026-10-14): several runs concatenated (segment_values).
    """
    if "segments" in spec:
        return segment_values(spec["segments"])[0]
    if "values" in spec:
        return native_values(spec["values"])
    if "path" in spec:
        try:
            return np.loadtxt(spec["path"]).ravel().astype(float)
        except ValueError:
            # not numbers: one setpoint per non-empty line (an enum's options)
            with open(spec["path"], encoding="utf-8") as fh:
                return native_values([ln.strip() for ln in fh if ln.strip()])
    start, stop = float(spec["start"]), float(spec["stop"])
    if spec.get("spacing", "linear") == "log":
        # LOG spacing (2026-10-12): the same ratio between neighbours, e.g. a
        # time constant 1 ms .. 1 s or a power 1 uW .. 10 mW in 7 points --
        # 1, 10, 100 ... instead of 1, 167, 334 ... Defined only between two
        # numbers of the same sign (a ratio cannot cross zero).
        if start == 0 or stop == 0 or (start < 0) != (stop < 0):
            raise ValueError(f"log spacing needs from and to of the same sign and "
                             f"neither 0 (got {start:g} .. {stop:g})")
        if spec.get("num") is None:
            raise ValueError("log spacing needs a number of points (num)")
        return np.geomspace(start, stop, int(spec["num"]))
    if spec.get("spacing", "linear") != "linear":
        raise ValueError(f"unknown spacing {spec['spacing']!r} (linear or log)")
    if "num" in spec and spec["num"] is not None:
        return np.linspace(start, stop, int(spec["num"]))
    step = float(spec["step"])
    n = int(round((stop - start) / step)) + 1
    return start + step * np.arange(max(1, n))


# ─────────────────────────── SEGMENTS (2026-10-14) ────────────────────────────
#
# A hysteresis measurement sweeps the field UP and then DOWN (and up again):
# the same values visited twice, in a given order. A `segments` axis is a
# list of linear runs, concatenated; the data file says which run every point
# belongs to, so a plot can draw the up and the down branch as two curves
# instead of one folded zig-zag line. docs/DEVELOPER_NOTES.md section 4l is
# the contract the plotting side (AaltoView, the live result) reads.

def segment_problems(segments) -> list[str]:
    """What is wrong with a `segments` list ([] = fine). Plain sentences:
    they are shown to the operator as they are."""
    if not isinstance(segments, list) or not segments:
        return ["segments must be a non-empty list of {start, stop, num}"]
    errs = []
    for k, seg in enumerate(segments, start=1):
        if not isinstance(seg, dict):
            errs.append(f"segment {k} is not a mapping of start, stop, num")
            continue
        try:
            a, b = float(seg.get("start")), float(seg.get("stop"))
        except (TypeError, ValueError):
            errs.append(f"segment {k}: start and stop must be numbers")
            continue
        if not (np.isfinite(a) and np.isfinite(b)):
            errs.append(f"segment {k}: start and stop must be finite numbers")
            continue
        try:
            n = int(seg.get("num"))
            if n != float(seg.get("num")):
                raise ValueError
        except (TypeError, ValueError):
            errs.append(f"segment {k}: the number of points must be a whole number")
            continue
        if n < 2:
            errs.append(f"segment {k} has {n} point(s); a segment needs at least 2 "
                        f"(from and to)")
        elif a == b:
            errs.append(f"segment {k} goes from {a:g} to {b:g}: from and to are "
                        f"equal, so it has no direction -- use a repeat axis to "
                        f"measure one value several times")
        sp = seg.get("spacing", "linear")
        if sp not in ("linear", "log"):
            errs.append(f"segment {k}: unknown spacing {sp!r} (linear or log)")
        elif sp == "log" and (a == 0 or b == 0 or (a < 0) != (b < 0)):
            errs.append(f"segment {k}: log spacing needs from and to of the same "
                        f"sign and neither 0 (got {a:g} .. {b:g})")
    return errs


def segment_values(segments) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """(values, segment index per point, one info dict per segment).

    Each segment is a linear (or `spacing: log`) run of `num` points from
    `start` to `stop`. When a segment STARTS on the value the previous one
    ENDED on, its first point is dropped -- the joint is measured once, as
    the last point of the earlier segment -- and its info says `joined: 1`.
    Info per segment: start, stop, num (as given), [spacing], direction (+1
    up / -1 down), joined (0/1), first (index of its first point in the
    axis) and count (its points in the axis: num, or num - 1 when joined).
    Raises ValueError with segment_problems' words on a bad list."""
    errs = segment_problems(segments)
    if errs:
        raise ValueError("; ".join(errs))
    vals: list[float] = []
    seg_of: list[int] = []
    info: list[dict] = []
    prev_end = None
    for k, seg in enumerate(segments):
        a, b, n = float(seg["start"]), float(seg["stop"]), int(seg["num"])
        sp = seg.get("spacing", "linear")
        run = np.geomspace(a, b, n) if sp == "log" else np.linspace(a, b, n)
        joined = prev_end is not None and a == prev_end
        if joined:
            run = run[1:]
        d = {"start": a, "stop": b, "num": n}
        if sp == "log":
            d["spacing"] = "log"
        d.update({"direction": 1 if b > a else -1, "joined": int(joined),
                  "first": len(vals), "count": int(len(run))})
        info.append(d)
        vals.extend(run.tolist())
        seg_of.extend([k] * len(run))
        prev_end = b
    return np.asarray(vals, dtype=float), np.asarray(seg_of, dtype=np.int32), info


def reversal_segments(values) -> tuple[np.ndarray, list[dict]] | None:
    """A hand-typed list that TURNS BACK (0, 50, 100, 50, 0, -50 ...) split
    into monotonic runs, so it is recorded and plotted like a `segments`
    axis. None for a list that never turns back (or is not numeric).

    A point belongs to the run of the step that LEADS INTO it, so the turning
    point (100 above) ends the up run and the down run starts after it --
    the same rule as a joined segment (every run after the first is
    `joined: 1`, its `start` the turning point). A step of zero (a value
    typed twice) does not change the direction."""
    v = np.asarray(values)
    if v.dtype == object or v.ndim != 1 or v.size < 3:
        return None
    try:
        v = v.astype(float)
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite(v)):
        return None
    seg = np.zeros(v.size, dtype=np.int32)
    dirs: list[int] = []
    k, cur = 0, 0
    for i in range(1, v.size):
        step = int(np.sign(v[i] - v[i - 1]))
        if step != 0 and cur != 0 and step != cur:
            k += 1
            dirs.append(cur)
        if step != 0:
            cur = step
        seg[i] = k
    dirs.append(cur if cur != 0 else 1)
    if k == 0:
        return None                              # monotonic: nothing to split
    info = []
    for j in range(k + 1):
        idx = np.flatnonzero(seg == j)
        first, last = int(idx[0]), int(idx[-1])
        start = float(v[first - 1]) if j > 0 else float(v[first])
        info.append({"start": start, "stop": float(v[last]),
                     "num": int(idx.size + (1 if j > 0 else 0)),
                     "direction": int(dirs[j]), "joined": int(j > 0),
                     "first": first, "count": int(idx.size)})
    return seg, info


def axis_segments(spec: dict, values) -> tuple[np.ndarray, list[dict], str] | None:
    """(segment index per point, info per segment, source) of one axis or
    zip / grid member, or None for an ordinary monotonic axis. source is
    "recipe" for a `segments` axis, "reversals" for a numeric list that
    turns back (reversal_segments)."""
    if not isinstance(spec, dict):
        return None
    if "segments" in spec:
        _, seg, info = segment_values(spec["segments"])
        return seg, info, "recipe"
    found = reversal_segments(values)
    if found is None:
        return None
    return found[0], found[1], "reversals"


def _with_segments(dim: "Dim", spec: dict) -> "Dim":
    """Attach the segments of `spec` (the dim's primary member) to `dim`."""
    if dim.params:
        found = axis_segments(spec, dim.params[0][1])
        if found is not None:
            dim.segment, dim.segments, dim.segment_source = found
    return dim


def _segment_like(last: dict, start: float, stop: float, num: int) -> dict:
    seg = {"start": start, "stop": stop, "num": int(num)}
    if last.get("spacing") == "log":
        seg["spacing"] = "log"
    return seg


def return_sweep(segments) -> list[dict]:
    """`segments` plus the MIRROR of its last run (to -> from, as many
    points): the builder's "+ return sweep". 0 -> 100 becomes 0 -> 100 -> 0."""
    segments = [dict(s) for s in segments or []]
    if not segments:
        return segments
    last = segments[-1]
    segments.append(_segment_like(last, float(last["stop"]), float(last["start"]),
                                  int(last["num"])))
    return segments


def hysteresis_loop(segments) -> list[dict]:
    """`segments` plus the DOWN and back-UP legs of a full loop over the last
    run's range: the builder's "+ loop", at the last run's step.

    a -> b becomes a -> b -> a -> b. A virgin curve from 0 (0 -> 100) is
    mirrored through zero, because that is what a hysteresis loop needs:
    0 -> 100 -> -100 -> 100 (the 200 mT legs with the same step as the
    first one)."""
    segments = [dict(s) for s in segments or []]
    if not segments:
        return segments
    last = segments[-1]
    a, b, n = float(last["start"]), float(last["stop"]), int(last["num"])
    far = -b if a == 0 else a                     # where the down leg turns
    if last.get("spacing") == "log" and (far == 0 or (far < 0) != (b < 0)):
        far = a                                   # a ratio cannot cross zero
    step = abs(b - a) / max(1, n - 1)
    m = int(round(abs(far - b) / step)) + 1 if step > 0 else n
    segments.append(_segment_like(last, b, far, m))
    segments.append(_segment_like(last, far, b, m))
    return segments


def segment_coords(dim: "Dim") -> dict:
    """The coordinates a segmented dim adds to the data file (the CONTRACT,
    docs/DEVELOPER_NOTES.md section 4l): {name: (dim, values, attrs)}, empty
    for a dim without segments."""
    if dim.segment is None or not dim.segments:
        return {}
    seg = np.asarray(dim.segment, dtype=np.int32)
    direction = np.asarray([dim.segments[k]["direction"] for k in seg], dtype=np.int8)
    return {
        f"{dim.name}_segment": (dim.name, seg, {
            "long_name": f"segment of {dim.name} (0 = first)",
            "units": "", "segment_of": dim.name}),
        f"{dim.name}_direction": (dim.name, direction, {
            "long_name": f"direction of {dim.name} (+1 up, -1 down)",
            "units": "", "segment_of": dim.name}),
    }


def segment_attrs(dim: "Dim") -> dict:
    """The attributes a segmented dim's own coordinate gets (the contract)."""
    if dim.segment is None or not dim.segments:
        return {}
    return {"segments_json": json.dumps(dim.segments),
            "segment_source": dim.segment_source or "recipe",
            "n_segments": int(len(dim.segments))}


# ─────────────────────────────── the recipe ───────────────────────────────────

@dataclass
class Recipe:
    name: str = "scan"
    comment: str = ""
    fixed: dict = field(default_factory=dict)     # params set ONCE before sweeping (context)
    axes: list[dict] = field(default_factory=list)
    detectors: list[str] = field(default_factory=list)
    hooks: list[dict] = field(default_factory=list)
    output: dict = field(default_factory=lambda: {"dir": ".", "basename": "scan", "format": "netcdf"})
    #: At a point where SEVERAL axes change (the start of a new row), send
    #: every new setpoint first and only then wait for them all, instead of
    #: one axis after the other (2026-10-08, from the first rig test of the XY
    #: mask): the camera then goes diagonally to (first column, next row)
    #: instead of settling at (last column, next row) on the way -- one
    #: wasted stabiliser settle per row. OFF by default: two moves at once is
    #: only safe where the instruments allow it (a KIM101 moving two channels
    #: together is not verified). Knobs that cannot split send/wait are set
    #: one after the other as before.
    diagonal: bool = False
    #: WINDOW (window.py, 2026-09-28; any physics since 2026-10-12), or None
    #: = off. A slow array detector sweeps only a band around the peak a
    #: PREDICTOR (predictors.py: kittel / linear / expression / a lab's own)
    #: expects; the rest of each trace is filled from the last full sweep's
    #: baseline and marked in a `<det>_measured` mask. Opt-in: a recipe
    #: without it runs exactly as before.
    window: dict | None = None
    #: SCOUT PASS (scout.py, 2026-10-08), or None = off. A quick look -- one
    #: scalar detector at every k-th point of the scouted axes -- first; the
    #: real scan then visits only the points where it saw something, and the
    #: rest are stored as not measured. Opt-in, like the window.
    scout: dict | None = None
    #: The XY MASK of 2026-10-07, READ ONLY: a recipe (a .yaml, or the
    #: recipe_json inside an older .nc) that still says `mask:` is translated
    #: into `scout` when it is made (scout.from_mask), and `mask` is then None
    #: -- so a definition saved again says `scout:`, and nothing reads two
    #: blocks that could disagree.
    mask: dict | None = None

    def __post_init__(self):
        # `raster` -> `grid` (2026-10-12), before anything reads the axes
        self.axes = upgrade_axes(self.axes)
        # the FMR-only window block (until 2026-10-12) -> a `kittel`
        # predictor, so a definition saved again says `predictor:`
        from .window import upgrade as upgrade_window
        self.window = upgrade_window(self.window)
        if self.mask and not self.scout:
            from .scout import from_mask
            self.scout = from_mask(self.mask, self.axes)
        self.mask = None

    # ---- (de)serialization ------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict, registry=None) -> "Recipe":
        """A recipe from its dict. With a `registry`, what an old recipe said
        by name is translated by role at once (migrate_roles); a problem
        there raises ValueError. Without one, validate() does it later."""
        r = cls._from_dict(d)
        if registry is not None:
            errs = r.migrate_roles(registry)
            if errs:
                raise ValueError("; ".join(errs))
        return r

    @classmethod
    def _from_dict(cls, d: dict) -> "Recipe":
        known = {f: d[f] for f in ("name", "comment", "fixed", "axes", "detectors",
                                   "hooks", "output", "window",
                                   "scout", "mask", "diagonal")
                 if f in d}
        # An old definition's `settle: {default_timeout_s}` (written until
        # 2026-10-12, read by nothing: a knob waits for its module's describe
        # timeout_s, 60 s when undeclared) is simply not picked up above, so
        # it is dropped and never written again.
        if "axes" in known:
            known["axes"] = upgrade_axes(known["axes"])
        if d.get("zigzag") is True and "axes" in known:
            # an OLD definition (or the recipe_json of an older .nc) with the
            # scan-wide switch: translated ONCE, here, into the per-axis flags
            known["axes"] = upgrade_scan_wide_zigzag(known["axes"])
        return cls(**known)

    def to_dict(self) -> dict:
        d = asdict(self)
        # No window = no key at all: a recipe that does not use the window is
        # written (and stored in every .nc as recipe_json) exactly as before.
        if not d.get("window"):
            d.pop("window", None)
        if not d.get("scout"):
            d.pop("scout", None)           # the same for the scout pass
        d.pop("mask", None)                # only ever READ (translated to scout)
        if not d.get("diagonal") or stepped_dims(self.axes) < 2:
            # ... and for diagonal row changes -- also when the flag is on
            # but cannot apply (fewer than two stepped dims: no row change)
            d.pop("diagonal", None)
        return d

    @classmethod
    def load(cls, path: str | Path, registry=None) -> "Recipe":
        # encoding="utf-8" explicitly: without it Windows reads with its code
        # page (cp1252), a "—" in a comment comes back as "â€”", and that
        # garbled text is then written into every measurement file made from it.
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh), registry=registry)

    def save(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.to_dict(), fh, sort_keys=False, allow_unicode=True)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    # ---- validation + compilation ----------------------------------------
    def migrate_roles(self, registry) -> list[str]:
        """Translate what an OLD recipe said by NAME into what the connected
        modules declare by ROLE. Today one thing: the built-in `autofocus`
        hook (until 2026-10-12) becomes a `call` routine running the action
        whose describe says `role: autofocus` (hooks.migrate_builtin_autofocus).
        Changes the recipe IN PLACE (a definition saved again says `call`).
        Returns the problems -- an old hook with no such action connected is
        refused, never run without its focusing. validate() and the engine
        call this first; Recipe.from_dict/load do when given a registry."""
        from .hooks import migrate_builtin_autofocus
        self.hooks, errs = migrate_builtin_autofocus(self.hooks, registry)
        return errs

    def validate(self, registry) -> list[str]:
        """Return a list of human-readable problems ([] means valid)."""
        errs: list[str] = self.migrate_roles(registry)
        for i, ax in enumerate(self.axes):
            if isinstance(ax, dict) and "zigzag" in ax:
                if not isinstance(ax["zigzag"], bool):
                    errs.append(f"axis {i + 1}: zigzag must be true or false, "
                                f"not {ax['zigzag']!r}")
                elif ax["zigzag"] and ax.get("type") == "repeat":
                    errs.append(f"axis {i + 1}: a repeat cannot zig-zag (it "
                                f"moves nothing)")
        for ax in self.axes:
            for pid in _axis_param_ids(ax):
                p = registry.get(pid)
                if p is None:
                    errs.append(f"axis references unknown parameter '{pid}'")
                elif p.kind != "settable":
                    errs.append(f"axis parameter '{pid}' is not settable")
        swept = {pid for ax in self.axes for pid in _axis_param_ids(ax)}
        for pid, value in self.fixed.items():
            p = registry.get(pid)
            if p is None:
                errs.append(f"fixed references unknown parameter '{pid}'")
                continue
            if p.kind != "settable":
                errs.append(f"fixed parameter '{pid}' is not settable")
                continue
            if pid in swept:
                # It would "work" -- set once, then swept over -- but the
                # condition recorded in the file would be a value the
                # measurement spent almost none of its time at.
                errs.append(f"'{pid}' is both a condition and an axis; "
                            f"it cannot be held and swept at the same time")
            if not _numeric(p):
                # an enum / string knob held at one value: one of its options
                try:
                    p.option(value)
                except (ValueError, AttributeError) as exc:
                    errs.append(f"condition {exc}")
                continue
            try:
                finite = np.isfinite(float(value))
            except (TypeError, ValueError):
                # a hand-edited definition ("10 mT"): report it like every
                # other problem instead of raising out of validate()
                errs.append(f"condition '{pid}' = {value!r} is not a number")
                continue
            if not finite:
                errs.append(f"condition '{pid}' is not a finite number")
                continue
            lo, hi = getattr(p, "limits", (None, None))
            if lo is not None and not (lo <= float(value) <= hi):
                errs.append(f"condition '{pid}' = {float(value):g} is outside "
                            f"its limits [{lo:g},{hi:g}]")
        for det in self.detectors:
            p = registry.get(det)
            if p is None:
                errs.append(f"detector references unknown parameter '{det}'")
            elif not hasattr(p, "get"):
                # A Settable can be a detector too: recording the readback of a
                # knob you are driving is normal (the magnet current while you
                # sweep field). What disqualifies a parameter is having no way
                # to read it at all -- a write-only control, or an action.
                errs.append(f"detector '{det}' cannot be read")
            elif getattr(p, "dtype", "float") == "text":
                # An UNTYPED text value (the old marker, before 2026-10-04):
                # the engine has no storage for it, and the first point would
                # crash the scan after the before-scan routine had moved
                # things. Enum and string detectors -- typed by describe --
                # ARE recorded since then (storage.py); they stay refused as
                # axes, because they are not settables.
                errs.append(f"detector '{det}' is text, not a number; it cannot "
                            f"be recorded in the data")
        seg_errs = self._validate_segments(registry)
        errs += seg_errs
        errs += self._validate_names(registry)
        errs += self._validate_hooks(registry)
        from .flyscan import validate_fly
        errs += validate_fly(self, registry)
        from .window import validate as validate_window
        errs += validate_window(self, registry)
        from .repeat import validate as validate_repeat
        errs += validate_repeat(self, registry)
        from .scout import validate as validate_scout
        errs += validate_scout(self, registry)
        # range check against each settable's limits
        try:
            for dim in self.compile(registry).dims:
                for pid, vals in dim.params:
                    p = registry.get(pid)
                    if len(vals) == 0:
                        # an empty list (nothing picked, an empty file): a
                        # scan of no points would "finish" at once, saving nothing
                        errs.append(f"axis '{dim.name}' has no values")
                        continue
                    text_vals = is_text_values(vals)
                    if p is not None and (text_vals or not _numeric(p)):
                        errs += _check_text_axis(p, vals, text_vals)
                        continue
                    # A non-finite setpoint passes every limit check (inf is not
                    # > inf) and then goes on the wire as the JSON token
                    # `Infinity`, which is not valid JSON. It gets here from an
                    # UNBOUNDED parameter: a module that advertises no min/max
                    # gives limits of (-inf, inf), and any span computed from
                    # those is infinite. Catch it before it is a setpoint.
                    if not np.all(np.isfinite(vals)):
                        errs.append(
                            f"'{pid}' sweep contains non-finite values "
                            f"(inf/nan). This usually means the parameter "
                            f"advertises no limits, so a span derived from them "
                            f"is infinite -- give the axis explicit from/to.")
                        continue
                    lo, hi = getattr(p, "limits", (None, None))
                    if lo is not None and (vals.min() < lo or vals.max() > hi):
                        errs.append(f"'{pid}' sweep [{vals.min():g},{vals.max():g}] "
                                    f"exceeds limits [{lo:g},{hi:g}]")
        except Exception as exc:                       # compile problems surface here
            # (a bad segments list is already reported by _validate_segments,
            # in the axis's words: not twice)
            if not seg_errs:
                errs.append(f"compile error: {exc}")
        return errs

    def _validate_segments(self, registry) -> list[str]:
        """A `segments` axis (2026-10-14): a list of {start, stop, num} runs
        over a NUMBER knob, on a stepped linear axis (or a zip / grid member),
        given instead of start / stop / num -- never next to them."""
        errs: list[str] = []
        for i, ax in enumerate(self.axes or []):
            if not isinstance(ax, dict):
                continue
            t = ax.get("type")
            specs = []
            if t == "zip":
                specs = [m for m in ax.get("members") or [] if isinstance(m, dict)]
            elif t in ("grid", "raster"):
                specs = [ax.get(k) for k in ("x", "y") if isinstance(ax.get(k), dict)]
            else:
                specs = [ax]
            for spec in specs:
                if "segments" not in spec:
                    continue
                where = f"axis '{spec.get('name') or spec.get('param') or i + 1}'"
                if t not in ("linear", "zip", "grid", "raster"):
                    errs.append(f"{where}: segments are given on a linear axis "
                                f"(type: linear), not on a {t} axis"
                                + (" -- a flown row is one continuous move"
                                   if t == "fly" else ""))
                    continue
                both = [k for k in ("start", "stop", "num", "step", "values", "path",
                                    "spacing") if k in spec]
                if both:
                    errs.append(f"{where}: give either segments or "
                                f"{', '.join(both)}, not both")
                p = registry.get(spec.get("param")) if registry is not None else None
                if p is not None and not _numeric(p):
                    errs.append(f"{where}: {p.id} is {getattr(p, 'dtype', 'text')}, not "
                                f"a number; segments run from one number to another "
                                f"-- give its values as a list")
                    continue
                errs += [f"{where}: {m}" for m in segment_problems(spec["segments"])]
        return errs

    def _validate_names(self, registry) -> list[str]:
        """Every dimension and coordinate of the data file needs its OWN name.

        Two axes called the same (the same parameter swept twice, or a `name`
        typed twice) run the WHOLE scan and then fail to become a dataset --
        the measured points are lost and the after-scan routine never runs
        (found 2026-09-28). The same goes for a zip member or a detector's own
        axis (a VNA's frequency) that shares a name with a scan dimension.
        """
        try:
            dims = self.compile(registry).dims
        except Exception:
            return []                          # the compile check reports it
        errs, seen = [], set()
        dim_names = [d.name for d in dims]
        for name in dim_names:
            if name in seen:
                errs.append(f"two axes are both called '{name}' in the data: "
                            f"sweep a parameter on one axis only, or give one "
                            f"of them a different name")
            seen.add(name)
        params = [pid for d in dims for pid, _ in d.params]
        for pid in sorted({p for p in params if params.count(p) > 1}):
            if pid not in dim_names:          # (else already reported above)
                errs.append(f"'{pid}' is swept by two axes; the outer one's "
                            f"coordinates would not be where it was measured")
        for d in dims:
            for pid, _ in d.params[1:]:       # zip members ride along as coords
                if pid in seen:
                    errs.append(f"zip member '{pid}' has the same name as a scan "
                                f"dimension; name the zip axis differently")
                seen.add(pid)
        det_axes = set()
        for det in self.detectors:
            for ax in getattr(registry.get(det), "axes", None) or []:
                det_axes.add(ax.name)
        for name in sorted(det_axes & seen):
            errs.append(f"a detector's own axis '{name}' has the same name as a "
                        f"scan dimension; name the scan axis differently")
        return errs

    def _validate_hooks(self, registry) -> list[str]:
        """Problems in the hooks, above all in the ROUTINES (`call` hooks).

        A routine is checked like a condition, because it IS a setpoint -- just
        one that is held for a moment instead of for the whole scan. It is worth
        refusing up front: the before-scan routine runs first, and a typo found
        only when it fires has already moved the magnet somewhere.
        """
        from .hooks import ACTIONS, EDGES, LEGACY_AUTOFOCUS, MOMENTS, ON_ERROR, describe_trigger
        errs: list[str] = []
        try:
            dim_names = [d.name for d in self.compile(registry).dims]
        except Exception:
            dim_names = None                 # the axis checks report that one
        for h in self.hooks or []:
            if not isinstance(h, dict):
                errs.append(f"hook {h!r} is not a mapping")
                continue
            when, name = h.get("when"), h.get("action")
            if when not in MOMENTS:
                # Otherwise a misspelt moment is a routine that never runs,
                # silently -- the scan "works" without its reference.
                errs.append(f"hook '{name}' has unknown moment {when!r} "
                            f"(one of: {', '.join(MOMENTS)})")
            if (h.get("on_error") or "stop") not in ON_ERROR:
                errs.append(f"hook '{name}': on_error must be one of "
                            f"{', '.join(ON_ERROR)}")
            if when == "every_n_points":
                try:
                    ok = int(h.get("n")) >= 1
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    errs.append(f"hook '{name}' every_n_points needs n >= 1")
            if when == "each_sweep":
                # The axis is a DIM name, as in the data file: a grid gives
                # two of them. Checked here because a routine bound to an axis
                # that was since removed from the stack would never fire.
                if dim_names is not None and h.get("axis") not in dim_names:
                    errs.append(f"routine at {describe_trigger(h)}: there is no "
                                f"axis {h.get('axis')!r} in this scan")
                if (h.get("edge") or "start") not in EDGES:
                    errs.append(f"hook '{name}': edge must be start or end")
                try:
                    every = h.get("every")
                    ok = every is None or int(every) >= 1
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    errs.append(f"hook '{name}': every must be a whole number >= 1")
            if name == LEGACY_AUTOFOCUS:
                continue                     # reported by migrate_roles()
            if name not in ACTIONS:
                errs.append(f"hook action {name!r} is not known "
                            f"(one of: {', '.join(sorted(ACTIONS))})")
                continue
            from .hooks import STEP_KINDS, routine_steps, step_problems
            if name in STEP_KINDS:
                # a generic step written as a hook of its own: the same as a
                # routine of that one step
                args = {"steps": [{name: h.get("args") or {}}]}
            elif name != "call":
                continue
            else:
                args = h.get("args") or {}
            where = f"{describe_trigger(h)} routine"
            # One reader for both spellings ({set, action} and {steps: [...]}),
            # the same one the hook runs from -- a checker that read the steps
            # differently from the runner would pass a routine that then fails.
            try:
                steps = routine_steps(args)
            except ValueError as exc:
                errs.append(f"{where}: {exc}")
                continue
            fly = any(isinstance(ax, dict) and ax.get("type") == "fly"
                      for ax in self.axes or [])
            for kind, pid, *rest in steps:
                if kind in STEP_KINDS:
                    # the five generic steps (2026-10-04): every expression is
                    # parsed and every id checked HERE, before anything moves
                    if not isinstance(pid, dict):
                        errs.append(f"{where}: {kind} needs a mapping")
                        continue
                    errs += [f"{where}: {msg}" for msg in
                             step_problems(kind, pid, registry, when, fly=fly)]
                    continue
                if kind == "action":
                    get_action = getattr(registry, "get_action", None)
                    act = get_action(pid) if get_action is not None else None
                    if act is None:
                        errs.append(f"{where} references unknown action '{pid}'")
                    elif rest:
                        # the step's arguments, against what the module declared
                        from .hooks import action_arg_problems
                        errs += [f"{where}: {m}" for m in action_arg_problems(act, rest[0])]
                    continue
                value = rest[0]
                p = registry.get(pid)
                if p is None:
                    errs.append(f"{where} references unknown parameter '{pid}'")
                    continue
                if p.kind != "settable":
                    errs.append(f"{where}: '{pid}' is not settable")
                    continue
                if not _numeric(p):
                    try:
                        p.option(value)
                    except (ValueError, AttributeError) as exc:
                        errs.append(f"{where}: {exc}")
                    continue
                try:
                    v = float(value)
                except (TypeError, ValueError):
                    errs.append(f"{where}: '{pid}' = {value!r} is not a number")
                    continue
                if not np.isfinite(v):
                    errs.append(f"{where}: '{pid}' is not a finite number")
                    continue
                lo, hi = getattr(p, "limits", (None, None))
                if lo is not None and not (lo <= v <= hi):
                    errs.append(f"{where}: '{pid}' = {v:g} is outside its "
                                f"limits [{lo:g},{hi:g}]")
        return errs

    def compile(self, registry=None) -> CompiledScan:
        dims: list[Dim] = []
        for ax in self.axes:
            dims.extend(_compile_axis(ax))
        from .repeat import name_dims
        name_dims(dims)                   # repeat / repeat_1, repeat_2, ...
        return CompiledScan(dims=dims, detectors=list(self.detectors), hooks=list(self.hooks))


def _numeric(p) -> bool:
    from .registry import is_numeric_param
    return is_numeric_param(p)


def _check_text_axis(p, vals, text_vals: bool) -> list[str]:
    """An axis over a NON-numeric knob (an enum's options, text), or text
    values given to a numeric knob ([] = fine)."""
    pid = p.id
    if _numeric(p):
        bad = [v for v in vals if not _is_number(v)]
        return [f"'{pid}' takes numbers; {bad[0]!r} is not one"] if bad else []
    if not text_vals and len(vals) and not getattr(p, "options", None):
        # a linear sweep over a string knob: numbers it cannot mean
        return [f"'{pid}' is {getattr(p, 'dtype', 'text')}: give its values as a "
                f"list (an array axis), not a numeric sweep"]
    errs = []
    for v in vals:
        try:
            p.option(v)
        except (ValueError, AttributeError) as exc:
            errs.append(str(exc))
            break
    return errs


# ─────────────────────────── zig-zag, per axis (2026-10-11) ───────────────────

def _axis_n_dims(ax: dict) -> int:
    """How many dims an axis compiles to (a grid: two), without compiling
    it (a `file` axis would read its file)."""
    return 2 if isinstance(ax, dict) and _axis_type(ax) == "grid" else 1


def stepped_dims(axes) -> int:
    """How many STEPPED dims the axes make: a grid two, a flown axis and a
    repeat none (they set nothing between points), anything else one. A
    `diagonal` row change needs at least two -- with fewer there is no row
    change to make diagonal, so the flag is not written (Recipe.to_dict)
    and the builder hides it."""
    n = 0
    for ax in axes or []:
        if not isinstance(ax, dict) or ax.get("type") in ("fly", "repeat"):
            continue
        n += _axis_n_dims(ax)
    return n


def dim_zigzag(axes) -> list[bool]:
    """One flag per compiled dim: is this dim swept backwards on every other
    sweep? An axis's `zigzag` applies to every dim it makes (both dims of a
    grid); a repeat never zig-zags (it moves nothing, and reversing it would
    only shuffle which repeat is which). The flag of dim 0 has no effect."""
    out: list[bool] = []
    for ax in axes or []:
        on = (isinstance(ax, dict) and ax.get("zigzag") is True
              and ax.get("type") != "repeat")
        out += [bool(on)] * _axis_n_dims(ax)
    return out


def upgrade_axes(axes) -> list:
    """An OLD axis list in today's words: a `raster` axis (until
    2026-10-12: "spatial XY") is a `grid` -- the same 2-D grid of any two
    knobs, renamed because nothing in it is spatial. Returns new dicts for
    the axes it changed; the others are the same objects. Not a list: as is."""
    if not isinstance(axes, list):
        return axes
    return [{**ax, "type": "grid"} if isinstance(ax, dict) and ax.get("type") == "raster"
            else ax for ax in axes]


def upgrade_scan_wide_zigzag(axes) -> list:
    """The OLD scan-wide `zigzag: true` (before 2026-10-11) as per-axis flags:
    `zigzag: true` on every axis that has a dim inside the outermost one --
    every axis but the first, and also the first when it is a grid (its
    fast dim is inner). A repeat gets none (it moves nothing). The ORDER is
    the per-axis rule's: the same as the old one in 2-D; in 3-D and up it
    saves the extra fly-back per outer step the old rule made when a middle
    axis had an even number of points. Returns new axis dicts (the input is
    not changed)."""
    out, first_dim = [], 0
    for ax in axes or []:
        n = _axis_n_dims(ax)
        if (isinstance(ax, dict) and ax.get("type") != "repeat"
                and first_dim + n - 1 >= 1):
            ax = {**ax, "zigzag": True}
        out.append(ax)
        first_dim += n
    return out


def zigzag_dim_names(recipe) -> list[str]:
    """The names of the dims that really zig-zag (dim 0 never does), for the
    summary and the file. Names as compile() gives them, without a registry."""
    try:
        dims = recipe.compile().dims
    except Exception:
        return []
    flags = dim_zigzag(getattr(recipe, "axes", None) or [])
    return [d.name for k, (d, f) in enumerate(zip(dims, flags)) if f and k > 0]


# ─────────────────────────── axis → Dim(s) helpers ────────────────────────────

def _axis_type(ax: dict) -> str:
    """An axis dict's type, with the OLD name `raster` read as `grid`: a
    single axis dict can reach the helpers below without going through a
    Recipe (the builder's rows, the axis preview)."""
    t = ax["type"]
    return "grid" if t == "raster" else t


def _axis_param_ids(ax: dict) -> list[str]:
    t = _axis_type(ax)
    if t == "repeat":
        return []                         # sets nothing
    if t in ("linear", "array", "file"):
        return [ax["param"]]
    if t == "fly":
        # the speed knob too: the scan drives it, so it must be settable and
        # cannot also be held as a condition
        return ([ax["param"]] + ([ax["speed_param"]] if ax.get("speed_param") else [])
                + ([ax["move"]] if ax.get("move") else []))
    if t == "zip":
        return [m["param"] for m in ax["members"]]
    if t == "grid":
        return [ax["x"]["param"], ax["y"]["param"]]
    raise ValueError(f"unknown axis type {t!r}")


def _compile_axis(ax: dict) -> list[Dim]:
    t = _axis_type(ax)
    if t == "repeat":
        from .repeat import interval_of, mode_of, num_of
        n = num_of(ax)
        # name "" = named by Recipe.compile (repeat, or repeat_1/_2 if several)
        return [Dim(name=ax.get("name") or "", params=[], size=n, kind="repeat",
                    values=np.arange(n), mode=mode_of(ax),
                    interval_s=interval_of(ax))]
    if t in ("linear", "array", "file"):
        vals = _values_from_spec(ax)
        name = ax.get("name") or ax["param"]
        return [_with_segments(Dim(name=name, params=[(ax["param"], vals)],
                                   size=len(vals), kind=t), ax)]

    if t == "fly":
        vals = _values_from_spec({k: ax[k] for k in ("start", "stop", "num")
                                  if k in ax})
        name = ax.get("name") or ax["param"]
        return [Dim(name=name, params=[(ax["param"], vals)], size=len(vals), kind="fly")]

    if t == "zip":
        members = ax["members"]
        vecs = [(m["param"], _values_from_spec(m)) for m in members]
        n = len(vecs[0][1])
        if any(len(v) != n for _, v in vecs):
            raise ValueError("zip members must have equal length")
        name = ax.get("name") or "+".join(p for p, _ in vecs)
        return [_with_segments(Dim(name=name, params=vecs, size=n, kind="zip"),
                               members[0])]

    if t == "grid":
        xv = _values_from_spec(ax["x"]);  xp = ax["x"]["param"]
        yv = _values_from_spec(ax["y"]);  yp = ax["y"]["param"]
        xname = ax["x"].get("name") or xp
        yname = ax["y"].get("name") or yp
        xdim = _with_segments(Dim(name=xname, params=[(xp, xv)], size=len(xv),
                                  kind="grid_x"), ax["x"])
        ydim = _with_segments(Dim(name=yname, params=[(yp, yv)], size=len(yv),
                                  kind="grid_y"), ax["y"])
        # `fast` axis becomes the INNER (last) dim so it varies quickest
        fast = ax.get("fast", "x")
        return [xdim, ydim] if fast == "y" else [ydim, xdim]

    raise ValueError(f"unknown axis type {t!r}")


# ─────────────────────── per-axis settings, in the file ───────────────────────

def axis_attrs(recipe, units=None, registry=None) -> dict:
    """{dimension name: {attribute: value}} -- each axis's ADVANCED settings
    (fly, scout), to be put on its coordinate in the data file.

    The whole recipe is in every file already (`recipe_json`), but a JSON blob
    is not what anyone reads in ncdump, MATLAB or Igor. "Was this row flown,
    and how fast?" should be answerable from the coordinate itself, next to
    its units. Only what is SET is written: a plain stepped axis gets nothing,
    so files of ordinary scans look exactly as before.

    `units(pid) -> str` gives a parameter's unit (the engine passes the
    registry's); without it the speed unit is left out.

    `registry` (the engine passes it) lets a fly axis over a knob with a RAMP
    block (flyscan.ramp_of, 2026-10-09) say how it was flown: its pace may
    come from row_time_s or the module's default (`fly_speed`), its rate unit
    is the ramp's, and `fly_binned_by` is "command" when the module could only
    record what it SENT. Every fly axis carries `fly_binned_by` (Lukas,
    2026-10-09): "measurement" (a stage's position, a Hall probe) or
    "command" (a generator's frequency) -- what the samples were sorted into
    pixels by, which a reader of the file must be able to tell apart.

    netCDF attributes cannot be booleans, so on/off is written as 1/0 -- except
    `fly`, which the fly engine has always written as the string "true" and
    readers already test for.
    """
    out: dict = {}
    units = units or (lambda _pid: "")
    for ax in getattr(recipe, "axes", None) or []:
        if not isinstance(ax, dict) or ax.get("type") != "fly":
            continue
        from .flyscan import fly_rate, ramp_of
        ramp = ramp_of(ax, registry) if registry is not None else None
        speed = fly_rate(ax, registry)
        if not np.isfinite(speed):
            continue                      # validate() reports a bad fly axis
        a = {"fly": "true", "fly_speed": float(speed)}
        moving = ax.get("move") or ax.get("param")
        unit = units(moving) if moving else ""
        if ramp is not None:
            # flown by the MODULE's sweep, at the ramp's rate unit
            a["fly_speed_units"] = ramp.rate_unit
            a["fly_mode"] = "ramp"
            a["fly_ramp"] = ramp.kind
        elif unit:
            # the speed is in the MOVING stage's unit per second (the stage
            # named by `move`, when the grid is in someone else's coordinates)
            a["fly_speed_units"] = f"{unit}/s"
        if ax.get("speed_param"):
            a["fly_speed_param"] = str(ax["speed_param"])
        if ax.get("move"):
            a["fly_move"] = str(ax["move"])
        if ax.get("readback"):
            a["fly_readback"] = str(ax["readback"])
        a["fly_binned_by"] = (ramp.binned_by if (ramp is not None and not ax.get("readback"))
                              else "measurement")
        if ax.get("row_time_s"):
            a["fly_row_time_s"] = float(ax["row_time_s"])
        a["fly_lag_correction"] = int(ax.get("lag_correction", True) is not False)
        if ax.get("timeout_s"):
            a["fly_timeout_s"] = float(ax["timeout_s"])
        # on a fly row zig-zag decides whether every other row was flown
        # backwards: the fly axis's own flag
        a["fly_zigzag"] = int(ax.get("zigzag") is True)
        # ONE MEAN PER ROW (flyscan.collapse_rows): said on the coordinate,
        # with whether the pixels were kept (only when it is on, so a file of
        # an ordinary fly scan looks exactly as before)
        from .flyscan import collapse_of, keeps_pixels
        if collapse_of(ax) == "mean":
            a["fly_collapse"] = "mean"
            a["fly_collapse_keep_pixels"] = int(keeps_pixels(ax))
        out.setdefault(ax.get("name") or ax.get("param"), {}).update(a)
    # ZIG-ZAG (2026-10-11): `zigzag = 1` on every coordinate that was swept
    # backwards on every other sweep -- only where it really happened (not on
    # the outermost dim, where it has no effect), so plain files are unchanged
    for name in zigzag_dim_names(recipe):
        out.setdefault(name, {})["zigzag"] = 1
    block = getattr(recipe, "scout", None)
    if isinstance(block, dict) and block:
        from .scout import spec_of
        try:
            spec = spec_of(block)
        except Exception:
            spec = None
        axes = spec.get("axes") if spec else None
        if isinstance(axes, dict):
            m = spec["margin"]
            measured = not spec["from"]
            for name, step in axes.items():
                a = out.setdefault(name, {})
                try:
                    k = int(step)
                except (TypeError, ValueError):
                    continue
                if measured:
                    # a picture or an earlier scan is not looked at every k-th
                    # point: its own pitch sets the grid, so no `every` then
                    a["scout_every"] = k
                v = m.get(name, "auto") if isinstance(m, dict) else m
                if v == "auto":
                    a["scout_margin"] = "auto"
                    if measured:
                        # what auto amounts to (scout.margin_radii): half the
                        # coarse step, in grid points
                        a["scout_margin_points"] = 0.5 * k
                else:
                    try:
                        a["scout_margin_points"] = float(v)
                    except (TypeError, ValueError):
                        pass
    return {k: v for k, v in out.items() if v}
