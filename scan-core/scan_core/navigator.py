"""Sample navigator: a design file (GDS / OASIS / image) registered to a stage.

The problem it solves: a sample is millimetres across, the camera sees a few
hundred micrometres of it, and the design file knows where everything is. Once
we know how DESIGN coordinates map onto STAGE coordinates, a click on the
design is a stage position.

Everything here is plain Python + numpy (no Qt), so it is unit-tested without
a display. The Navigator tab (apps/navigator.py) is a thin view on top.

Coordinates
-----------
* DESIGN frame: micrometres, y UP (the GDS convention). An image is placed
  with its centre at (0, 0) and its pixel size = real width / pixel width.
* STAGE frame: micrometres too. The tab converts to and from the stage's own
  unit (KIM reports um, the BSC203 mm) at the very edge, so no maths here ever
  sees two units.

The mapping is an affine transform  stage = M @ design + t  (+ a drift shift,
below). How much of it is FITTED depends on how many reference points you gave:

  0 points  the "prior": your rotation and mirror, scale 1, no offset.
  1 point   the prior's rotation/mirror, offset from the point. This is the
            "rotate it by eye, click once" workflow.
  2 points  a similarity: rotation + one scale + offset, exact. The design's
            scale is exact (GDS), so a fitted scale that is not 1.000 is the
            STAGE's scale error -- worth reading on an open-loop KIM.
  3 points  similarity by least squares; the residuals say how good it is.
            Mirror is detected here (whichever handedness fits better).
  4+        full affine (separate X/Y scale, skew) by least squares. KIM's X
            and Y step sizes are calibrated separately, so they can disagree.

Drift correction ("I am here, offset only")
-------------------------------------------
An open-loop stage (KIM) loses its position: on the rig ~26 um over a long
raster (2026-09-25). The fix is to look at a feature, click it, and say "I am
here" -- which shifts the whole mapping by the error but keeps its rotation and
scale. The shift is stored separately, and later reference points are stored
with the shift taken OFF, so they stay consistent with the earlier ones: the
shift models "the counter moved", not "the sample moved".
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# The stage's unit -> micrometres. What a module reports in `describe`.
UNIT_TO_UM = {"um": 1.0, "µm": 1.0, "μm": 1.0, "micron": 1.0, "mm": 1000.0,
              "nm": 1e-3, "m": 1e6}


def unit_factor(unit: str) -> float | None:
    """Micrometres per one `unit`, or None if we do not know the unit."""
    return UNIT_TO_UM.get((unit or "").strip())


# --------------------------------------------------------------------------- #
# the design
# --------------------------------------------------------------------------- #
@dataclass
class Layer:
    key: str                         # "43/0" = GDS layer/datatype
    polygons: list                   # list of (N, 2) arrays, design um
    visible: bool = True


@dataclass
class Design:
    """What was loaded. `kind` is "gds" (vector) or "image" (raster)."""

    kind: str
    path: str
    layers: dict = field(default_factory=dict)       # key -> Layer   (gds)
    cell: str = ""
    width_um: float = 0.0                             # (image) real width
    image_px: tuple = (0, 0)                          # (image) width, height in px

    @property
    def um_per_px(self) -> float:
        return self.width_um / self.image_px[0] if self.image_px[0] else 0.0

    def bbox(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) in design um."""
        if self.kind == "image":
            w = self.width_um
            h = self.image_px[1] * self.um_per_px
            return (-w / 2, -h / 2, w / 2, h / 2)
        pts = [p for L in self.layers.values() for p in L.polygons]
        if not pts:
            return (0.0, 0.0, 0.0, 0.0)
        allp = np.concatenate(pts)
        return (*allp.min(axis=0), *allp.max(axis=0))

    def image_pixel_to_design(self, px: float, py: float) -> tuple[float, float]:
        """Pixel (x right, y DOWN, from the top-left corner) -> design um."""
        s = self.um_per_px
        w, h = self.image_px
        return ((px - w / 2) * s, (h / 2 - py) * s)


def _pick_top_cell(lib):
    """The cell the user means: the top cell with the most geometry.

    KLayout writes a `$$$CONTEXT_INFO$$$` top cell holding its own PCell
    bookkeeping; it is never the sample.
    """
    tops = [c for c in lib.top_level() if not c.name.startswith("$$$")]
    if not tops:
        raise ValueError("the file has no top-level cell with geometry")
    return max(tops, key=lambda c: len(c.get_polygons()))


def load_gds(path: str | Path, cell: str | None = None) -> Design:
    """Read a GDSII or OASIS file into per-layer polygon lists (design um).

    Uses `gdstk` (pip, no KLayout install needed). References and arrays are
    flattened, paths are turned into polygons, so what you get is what KLayout
    draws.
    """
    try:
        import gdstk
    except ImportError as exc:        # pragma: no cover - dependency message
        raise ImportError("reading GDS needs gdstk: uv sync --extra gui") from exc
    path = Path(path)
    lib = (gdstk.read_oas(str(path)) if path.suffix.lower() in (".oas", ".oasis")
           else gdstk.read_gds(str(path)))
    if cell:
        found = [c for c in lib.cells if c.name == cell]
        if not found:
            raise ValueError(f"no cell {cell!r} in {path.name}")
        top = found[0]
    else:
        top = _pick_top_cell(lib)
    # The file's user unit (metres per unit) -> um. Usually 1e-6, i.e. 1.
    to_um = lib.unit / 1e-6
    layers: dict[str, Layer] = {}
    for poly in top.get_polygons(apply_repetitions=True, include_paths=True):
        key = f"{poly.layer}/{poly.datatype}"
        layers.setdefault(key, Layer(key, [])).polygons.append(
            np.asarray(poly.points, dtype=float) * to_um)
    ordered = dict(sorted(layers.items(),
                          key=lambda kv: tuple(int(x) for x in kv[0].split("/"))))
    return Design("gds", str(path), layers=ordered, cell=top.name)


def image_design(path: str | Path, width_px: int, height_px: int,
                 width_um: float) -> Design:
    """An image described by its real width (the pixels are read by the GUI)."""
    if width_um <= 0 or width_px <= 0 or height_px <= 0:
        raise ValueError("an image needs a positive size and a positive real width")
    return Design("image", str(path), width_um=float(width_um),
                  image_px=(int(width_px), int(height_px)))


# --------------------------------------------------------------------------- #
# the registration (design -> stage)
# --------------------------------------------------------------------------- #
def _rot(deg: float) -> np.ndarray:
    a = math.radians(deg)
    return np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])


def _mirror(on: bool) -> np.ndarray:
    # Mirror = flip the design's y before anything else. Any mirror is this one
    # followed by a rotation, so one flag covers "sample face down" and "a
    # stage axis runs the other way".
    return np.diag([1.0, -1.0 if on else 1.0])


def _fit_similarity(d: np.ndarray, s: np.ndarray, mirror: bool):
    """Least-squares  s = a * F d + b  with a complex `a` (rotation + scale).

    Complex numbers make this a one-liner: a rotation+scale is a multiplication.
    """
    F = _mirror(mirror)
    dm = d @ F.T
    z = dm[:, 0] + 1j * dm[:, 1]
    w = s[:, 0] + 1j * s[:, 1]
    zc, wc = z - z.mean(), w - w.mean()
    denom = np.sum(np.abs(zc) ** 2)
    if denom <= 0:
        raise ValueError("the reference points are all at the same design position")
    a = np.sum(np.conj(zc) * wc) / denom
    b = w.mean() - a * z.mean()
    M = np.array([[a.real, -a.imag], [a.imag, a.real]]) @ F
    t = np.array([b.real, b.imag])
    return M, t


def _fit_affine(d: np.ndarray, s: np.ndarray):
    A = np.hstack([d, np.ones((len(d), 1))])
    sol, *_ = np.linalg.lstsq(A, s, rcond=None)      # (3, 2)
    M = sol[:2].T
    t = sol[2]
    if abs(np.linalg.det(M)) < 1e-12:
        raise ValueError("the reference points are on one line; add one off it")
    return M, t


@dataclass
class RefPoint:
    design: tuple            # (x, y) design um
    stage: tuple             # (x, y) stage um, drift shift already REMOVED


@dataclass
class Registration:
    """Reference points + the prior -> the design->stage transform."""

    rotation_deg: float = 0.0        # prior, used with 0-1 points
    mirror: bool = False             # prior with 0-2 points; detected with 3+
    points: list = field(default_factory=list)
    shift: tuple = (0.0, 0.0)        # drift correction, stage um
    model: str = "auto"              # auto | similarity | affine

    # ---- editing ------------------------------------------------------ #
    def add_point(self, design_xy, stage_xy) -> None:
        """"This design point is under the laser now." Refits everything."""
        sx, sy = self.shift
        self.points.append(RefPoint((float(design_xy[0]), float(design_xy[1])),
                                    (float(stage_xy[0]) - sx, float(stage_xy[1]) - sy)))
        self._detect_mirror()

    def correct_offset(self, design_xy, stage_xy) -> tuple[float, float]:
        """Shift the mapping so `design_xy` lands on `stage_xy`; keep the rest.

        Returns the correction applied (stage um) -- the drift since the last
        reference. With no points yet this is the same as adding the first one.
        """
        if not self.points:
            self.add_point(design_xy, stage_xy)
            return (0.0, 0.0)
        px, py = self.to_stage(*design_xy)
        dx, dy = float(stage_xy[0]) - px, float(stage_xy[1]) - py
        self.shift = (self.shift[0] + dx, self.shift[1] + dy)
        return (dx, dy)

    def remove_point(self, index: int) -> None:
        del self.points[index]
        if not self.points:
            self.shift = (0.0, 0.0)

    def clear(self) -> None:
        self.points.clear()
        self.shift = (0.0, 0.0)

    def _detect_mirror(self) -> None:
        # Two points fit either handedness exactly; from three on, the wrong
        # one leaves a residual, so the data decides.
        if len(self.points) < 3:
            return
        d, s = self._arrays()
        res = {}
        for m in (False, True):
            M, t = _fit_similarity(d, s, m)
            res[m] = np.sum((d @ M.T + t - s) ** 2)
        self.mirror = bool(res[True] < res[False])

    # ---- the transform ------------------------------------------------ #
    def _arrays(self):
        d = np.array([p.design for p in self.points], dtype=float)
        s = np.array([p.stage for p in self.points], dtype=float)
        return d, s

    def fitted_model(self) -> str:
        n = len(self.points)
        if n == 0:
            return "prior"
        if n == 1:
            return "offset"
        if self.model == "similarity" or (self.model == "auto" and n < 4) or n < 3:
            return "similarity"
        return "affine"

    def transform(self) -> tuple[np.ndarray, np.ndarray]:
        """(M, t) with stage = M @ design + t, drift shift included."""
        prior = _rot(self.rotation_deg) @ _mirror(self.mirror)
        n = len(self.points)
        if n == 0:
            M, t = prior, np.zeros(2)
        elif n == 1:
            d, s = self._arrays()
            M, t = prior, s[0] - prior @ d[0]
        elif self.fitted_model() == "similarity":
            M, t = _fit_similarity(*self._arrays(), self.mirror)
        else:
            M, t = _fit_affine(*self._arrays())
        return M, t + np.asarray(self.shift, dtype=float)

    def to_stage(self, x: float, y: float) -> tuple[float, float]:
        M, t = self.transform()
        s = M @ np.array([x, y], dtype=float) + t
        return float(s[0]), float(s[1])

    def to_design(self, x: float, y: float) -> tuple[float, float]:
        M, t = self.transform()
        d = np.linalg.solve(M, np.array([x, y], dtype=float) - t)
        return float(d[0]), float(d[1])

    def residuals(self) -> list[float]:
        """Per point: how far (um) the fit puts it from where the stage was."""
        if not self.points:
            return []
        M, t = self.transform()
        d, s = self._arrays()
        s = s + np.asarray(self.shift, dtype=float)
        return [float(v) for v in np.hypot(*(d @ M.T + t - s).T)]

    def summary(self) -> dict:
        """What the fit says about the stage, in words a person checks."""
        M, t = self.transform()
        sx, sy = np.hypot(M[0, 0], M[1, 0]), np.hypot(M[0, 1], M[1, 1])
        rot = math.degrees(math.atan2(M[1, 0], M[0, 0]))
        # angle between the images of the design's x and y axes: 90 = square
        cosang = (M[:, 0] @ M[:, 1]) / (sx * sy) if sx and sy else 0.0
        res = self.residuals()
        return {
            "model": self.fitted_model(), "points": len(self.points),
            "rotation_deg": rot, "scale_x": sx, "scale_y": sy,
            "skew_deg": 90.0 - math.degrees(math.acos(max(-1, min(1, cosang)))),
            "mirror": bool(np.linalg.det(M) < 0),
            "rms_um": float(np.sqrt(np.mean(np.square(res)))) if res else 0.0,
            "max_um": max(res) if res else 0.0,
            "shift_um": tuple(self.shift),
        }

    # ---- save / load -------------------------------------------------- #
    def to_dict(self) -> dict:
        return {"rotation_deg": self.rotation_deg, "mirror": self.mirror,
                "model": self.model, "shift": list(self.shift),
                "points": [{"design": list(p.design), "stage": list(p.stage)}
                           for p in self.points]}

    @classmethod
    def from_dict(cls, d: dict) -> "Registration":
        r = cls(rotation_deg=float(d.get("rotation_deg", 0.0)),
                mirror=bool(d.get("mirror", False)),
                model=str(d.get("model", "auto")),
                shift=tuple(float(v) for v in d.get("shift", (0.0, 0.0))))
        r.points = [RefPoint(tuple(p["design"]), tuple(p["stage"]))
                    for p in d.get("points", [])]
        return r


# --------------------------------------------------------------------------- #
# which stage
# --------------------------------------------------------------------------- #
@dataclass
class StagePair:
    """An X and a Y settable that move the sample, found in a registry."""

    label: str               # "kim", "stage", "simulator"
    prefix: str              # "kim." -- also what the Stop button looks for
    x: object                # Settable
    y: object                # Settable
    um_per_unit: float       # 1000 for a stage in mm
    unit_known: bool = True

    def to_unit(self, um: float) -> float:
        return um / self.um_per_unit

    def limits_um(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) of the travel, in um."""
        f = self.um_per_unit
        return (self.x.limits[0] * f, self.y.limits[0] * f,
                self.x.limits[1] * f, self.y.limits[1] * f)

    def inside(self, x_um: float, y_um: float) -> bool:
        x0, y0, x1, y1 = self.limits_um()
        return x0 <= x_um <= x1 and y0 <= y_um <= y1


# Every stage module in the suite calls its axes position_x / position_y in
# `describe` (kim in um, the BSC203 in mm); the simulator has pos_x / pos_y.
# Matching on those names -- rather than listing modules -- means a new stage
# module appears here the day it is written, as long as it follows the names.
_XY_NAMES = (("position_x", "position_y"), ("pos_x", "pos_y"))


def stage_pairs(registry) -> list[StagePair]:
    """Every (X, Y) settable pair in `registry` that looks like a sample stage."""
    by_id = {p.id: p for p in registry.settables()}
    found = []
    for pid, p in by_id.items():
        for xname, yname in _XY_NAMES:
            if not (pid == xname or pid.endswith("." + xname)):
                continue
            prefix = pid[: -len(xname)]
            q = by_id.get(prefix + yname)
            if q is None:
                continue
            f = unit_factor(p.unit)
            found.append(StagePair(prefix.rstrip(".") or "simulator", prefix, p, q,
                                   f if f is not None else 1.0, f is not None))
    return sorted(found, key=lambda s: s.label)


# --------------------------------------------------------------------------- #
# moving there
# --------------------------------------------------------------------------- #
def waypoints(target_um, current_um, approach_um: float = 0.0) -> list[tuple]:
    """Where to stop on the way to `target_um` (stage um).

    With `approach_um` > 0 the LAST leg always runs in +X and +Y over that
    distance: an open-loop inertia stage steps differently each way and has
    slack, so arriving from the same side every time makes a position
    repeatable. The detour is skipped when we are already just below the target.
    """
    tx, ty = map(float, target_um)
    if approach_um <= 0:
        return [(tx, ty)]
    cx, cy = map(float, current_um)
    if tx - approach_um <= cx <= tx and ty - approach_um <= cy <= ty:
        return [(tx, ty)]
    return [(tx - approach_um, ty - approach_um), (tx, ty)]


# --------------------------------------------------------------------------- #
# session files
# --------------------------------------------------------------------------- #
SESSION_VERSION = 1


def save_session(path: str | Path, design: Design | None, reg: Registration,
                 extra: dict | None = None) -> None:
    """Design reference + registration as JSON, so tomorrow starts where you left.

    The design is saved as its PATH (and, for an image, its real width), not
    its contents: the GDS is the source of truth.
    """
    data = {"version": SESSION_VERSION, "registration": reg.to_dict(),
            "design": None, **(extra or {})}
    if design is not None:
        data["design"] = {"kind": design.kind, "path": design.path,
                          "cell": design.cell, "width_um": design.width_um,
                          "hidden_layers": [k for k, L in design.layers.items()
                                            if not L.visible]}
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_session(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["registration"] = Registration.from_dict(data.get("registration", {}))
    return data
