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
settle:    {default_timeout_s}
axes:      [Axis, ...]      # OUTER→INNER. axes[0] is the slowest (outermost) loop.
detectors: [param_id, ...]  # gettables recorded at every point
hooks:     [Hook, ...]       # optional actions bound to a level/cadence
zigzag:    bool              # serpentine order (off by default)
diagonal:  bool              # at a row change send all setpoints, then wait
window:    {...}             # optional RESONANCE WINDOW (window.py): sweep a slow
                             # array detector only around the predicted FMR line
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
linear : {type, param, start, stop, num, [name]}          -> 1 dim
array  : {type, param, values:[...], [name]}              -> 1 dim
file   : {type, param, path, [name]}                      -> 1 dim (values from CSV/txt)
zip    : {type, name, members:[{param, ...spec}], }       -> 1 dim, several params in lockstep
raster : {type, x:{param,start,stop,num}, y:{...}, fast}  -> 2 dims (spatial XY, first-class)
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
(raster expands to two dims), which the engine iterates as an odometer. Keeping
"how you describe a scan" (axes) separate from "how you run it" (dims) is what
lets XY imaging be one entry in the UI yet a genuine 2 dimensions in the data.
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

def _values_from_spec(spec: dict) -> np.ndarray:
    """Turn a per-axis/per-member spec into an explicit value vector.

    Accepts either {start, stop, num} (linear), {start, stop, step}, or
    {values:[...]} / {path:...}. One place so every axis type shares it.
    """
    if "values" in spec:
        return np.asarray(spec["values"], dtype=float)
    if "path" in spec:
        return np.loadtxt(spec["path"]).ravel().astype(float)
    start, stop = float(spec["start"]), float(spec["stop"])
    if "num" in spec and spec["num"] is not None:
        return np.linspace(start, stop, int(spec["num"]))
    step = float(spec["step"])
    n = int(round((stop - start) / step)) + 1
    return start + step * np.arange(max(1, n))


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
    settle: dict = field(default_factory=lambda: {"default_timeout_s": 20.0})
    #: Sweep every other pass of an inner axis BACKWARDS (boustrophedon), so the
    #: stage does not fly back to the start of each row. Saves the fly-back on a
    #: slow axis -- on a 20 x 20 camera-array scan, 19 returns across the sample.
    #: OFF by default: it is only safe where a point does not depend on the
    #: direction it was approached from, and an open-loop slip-stick stage with
    #: 40 % direction asymmetry, or any axis with backlash or hysteresis (the
    #: magnet), reaches a slightly different place coming the other way.
    zigzag: bool = False
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
    #: RESONANCE WINDOW (window.py, 2026-09-28), or None = off. A slow array
    #: detector sweeps only a band around the predicted FMR line; the rest of
    #: each trace is filled from the last full sweep's baseline and marked in a
    #: `<det>_measured` mask. Opt-in: a recipe without it runs exactly as before.
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
        if self.mask and not self.scout:
            from .scout import from_mask
            self.scout = from_mask(self.mask, self.axes)
        self.mask = None

    # ---- (de)serialization ------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict) -> "Recipe":
        known = {f: d[f] for f in ("name", "comment", "fixed", "axes", "detectors",
                                   "hooks", "output", "settle", "zigzag", "window",
                                   "scout", "mask", "diagonal")
                 if f in d}
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
        if not d.get("diagonal"):
            d.pop("diagonal", None)        # ... and for diagonal row changes
        return d

    @classmethod
    def load(cls, path: str | Path) -> "Recipe":
        # encoding="utf-8" explicitly: without it Windows reads with its code
        # page (cp1252), a "—" in a comment comes back as "â€”", and that
        # garbled text is then written into every measurement file made from it.
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh))

    def save(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.to_dict(), fh, sort_keys=False, allow_unicode=True)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    # ---- validation + compilation ----------------------------------------
    def validate(self, registry) -> list[str]:
        """Return a list of human-readable problems ([] means valid)."""
        errs: list[str] = []
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
            errs.append(f"compile error: {exc}")
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
        from .hooks import ACTIONS, EDGES, MOMENTS, ON_ERROR, describe_trigger
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
                # The axis is a DIM name, as in the data file: a raster gives
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
            if name not in ACTIONS:
                errs.append(f"hook action {name!r} is not known "
                            f"(one of: {', '.join(sorted(ACTIONS))})")
                continue
            if name == "autofocus":
                from .hooks import find_autofocus
                if find_autofocus(registry) is None:
                    errs.append(f"{describe_trigger(h)}: autofocus, but no module here "
                                f"offers an autofocus action (is the camera connected?)")
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
                    if get_action is None or get_action(pid) is None:
                        errs.append(f"{where} references unknown action '{pid}'")
                    continue
                value = rest[0]
                p = registry.get(pid)
                if p is None:
                    errs.append(f"{where} references unknown parameter '{pid}'")
                    continue
                if p.kind != "settable":
                    errs.append(f"{where}: '{pid}' is not settable")
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


# ─────────────────────────── axis → Dim(s) helpers ────────────────────────────

def _axis_param_ids(ax: dict) -> list[str]:
    t = ax["type"]
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
    if t == "raster":
        return [ax["x"]["param"], ax["y"]["param"]]
    raise ValueError(f"unknown axis type {t!r}")


def _compile_axis(ax: dict) -> list[Dim]:
    t = ax["type"]
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
        return [Dim(name=name, params=[(ax["param"], vals)], size=len(vals), kind=t)]

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
        return [Dim(name=name, params=vecs, size=n, kind="zip")]

    if t == "raster":
        xv = _values_from_spec(ax["x"]);  xp = ax["x"]["param"]
        yv = _values_from_spec(ax["y"]);  yp = ax["y"]["param"]
        xname = ax["x"].get("name") or xp
        yname = ax["y"].get("name") or yp
        xdim = Dim(name=xname, params=[(xp, xv)], size=len(xv), kind="raster_x")
        ydim = Dim(name=yname, params=[(yp, yv)], size=len(yv), kind="raster_y")
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
        # zig-zag is scan-wide in the recipe; on a fly row it decides whether
        # every other row was flown backwards, so it belongs here too
        a["fly_zigzag"] = int(bool(getattr(recipe, "zigzag", False)))
        out.setdefault(ax.get("name") or ax.get("param"), {}).update(a)
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
