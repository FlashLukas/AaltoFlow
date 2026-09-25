"""Camera-frame calibration of the XY stage: image PIXELS per STEP (maths + storage).

Measured by :mod:`kim.calibration` from camera feedback; used by
:meth:`Kim.move_image_px` so a client (the camera's stabiliser) can say "shift
the image by (dx, dy) pixels" without knowing how the stage is mounted.

------------------------------------------------------------------------------
The model
------------------------------------------------------------------------------
Each axis and DIRECTION has a column: the image displacement, in pixels, per
step moved in that direction::

    image_shift_px = c_X(sign sx) * sx + c_Y(sign sy) * sy      (sx, sy signed steps)

    c_X+ = d(image px)/d(steps) while X moves +,   a 2-vector (px_x, px_y)
    c_X- = the same slope while X moves -          (so -1 step shifts by -c_X-)

One column per direction because slip-stick steps are NOT the same size both
ways (lab rig 2026-09-13: Y forward 0.48 px/step, backward 0.36). The columns
together hold everything the camera needs:

  * their LENGTHS    -> step size (px/step; um/step once the pixel size is known)
  * their DIRECTIONS -> how the stage is mounted relative to the image (the lab
                       rig: X moves the image up, Y moves it right)
  * the ANGLE between the X and Y columns -> crosstalk / non-orthogonality

The step size also depends on the DRIVE VOLTAGE, so the table is per voltage and
:func:`columns_at` interpolates linearly between calibrated voltages.

Why pixels and not micrometres: the stabiliser measures its error in pixels, so a
px/step table closes that loop without the (separately calibrated) pixel size.
The pixel size is recorded alongside, for converting to um later.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from itertools import product
from pathlib import Path

import numpy as np

DIRS = ("X+", "X-", "Y+", "Y-")


@dataclass
class PxCalibration:
    """A px/step table plus what it was measured under."""

    # voltage (as a string key, JSON-friendly) -> direction -> [px_x, px_y] per step
    table: dict = field(default_factory=dict)
    # same shape: standard deviation of each column over the repeats
    spread: dict = field(default_factory=dict)
    # The image geometry the columns are only valid for. A client passes its own
    # when commanding a move; any mismatch is refused (new objective, rotated
    # image, different ROI -> the old table would move the stage wrongly).
    context: dict = field(default_factory=dict)
    pixel_size_um: float = 0.0     # camera's configured value at calibration time
    step_rate: float = 0.0         # steps/s used while measuring
    created: str = ""
    validation: list = field(default_factory=list)   # closed-loop move checks

    # -- queries ---------------------------------------------------------------
    def voltages(self) -> list[float]:
        return sorted(float(v) for v in self.table)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PxCalibration":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def save(cal: PxCalibration, path) -> None:
    Path(path).write_text(json.dumps(cal.to_dict(), indent=1), encoding="utf-8")


def load(path) -> PxCalibration | None:
    p = Path(path)
    if not p.is_file():
        return None
    return PxCalibration.from_dict(json.loads(p.read_text(encoding="utf-8")))


def _interp_column(cal: PxCalibration, direction: str, volts: float) -> np.ndarray:
    """One direction's column at ``volts``: linear between measured voltages,
    held at the end values outside the measured range (no extrapolation)."""
    vs = cal.voltages()
    if not vs:
        raise ValueError("calibration table is empty")
    cols = np.array([cal.table[_key(cal, v)][direction] for v in vs], float)
    if len(vs) == 1:
        return cols[0]
    return np.array([np.interp(volts, vs, cols[:, i]) for i in range(2)])


def _key(cal: PxCalibration, v: float) -> str:
    for k in cal.table:
        if abs(float(k) - v) < 1e-9:
            return k
    raise KeyError(v)


def columns_at(cal: PxCalibration, volts_x: float, volts_y: float) -> dict[str, np.ndarray]:
    """All four columns at the axes' CURRENT drive voltages (X and Y may differ)."""
    return {
        "X+": _interp_column(cal, "X+", volts_x),
        "X-": _interp_column(cal, "X-", volts_x),
        "Y+": _interp_column(cal, "Y+", volts_y),
        "Y-": _interp_column(cal, "Y-", volts_y),
    }


def solve_steps(shift_px, cols: dict[str, np.ndarray]) -> tuple[float, float]:
    """Signed (sx, sy) steps that shift the image by ``shift_px``.

    Because the columns depend on the direction of each axis, the system is
    piecewise linear: try each of the four sign combinations, solve the 2x2, and
    keep the solution whose signs agree with the columns that produced it. For
    a stage whose axes are anywhere near orthogonal exactly one combination is
    consistent. If none is (a degenerate table), take the smallest residual.
    """
    d = np.asarray(shift_px, float)
    best, best_res = (0.0, 0.0), np.inf
    for sx_sign, sy_sign in product((+1, -1), repeat=2):
        m = np.column_stack([cols["X+" if sx_sign > 0 else "X-"],
                             cols["Y+" if sy_sign > 0 else "Y-"]])
        if abs(np.linalg.det(m)) < 1e-12:
            continue
        sx, sy = np.linalg.solve(m, d)
        consistent = (sx * sx_sign >= -1e-9) and (sy * sy_sign >= -1e-9)
        if consistent:
            return float(sx), float(sy)
        res = np.linalg.norm(m @ [sx, sy] - d)
        if res < best_res:
            best, best_res = (float(sx), float(sy)), res
    return best


def um_per_step(cal: PxCalibration, axis: int, direction: int,
                volts_x: float, volts_y: float) -> float | None:
    """MEASURED step size of X (axis 0) or Y (axis 1), in um, from the camera.

    The table is image PIXELS per step; multiplying by the camera's pixel size
    at calibration time (stored in the file) turns it into micrometres. It is
    per DIRECTION, because a slip-stick actuator does not step equally both
    ways -- on this rig Y- was 1.7x Y+ at 85 V. `direction` > 0 = forward,
    < 0 = backward, 0 = the mean of the two (the honest single number for a
    readout or a speed, where no direction is known yet).

    None when this axis has no camera calibration (Z), or the file carries no
    pixel size -- the caller then falls back to the configured um_per_step.
    """
    if axis not in (0, 1) or not cal.table or cal.pixel_size_um <= 0:
        return None
    name = "X" if axis == 0 else "Y"
    cols = columns_at(cal, volts_x, volts_y)
    fwd = float(np.hypot(*cols[f"{name}+"])) * cal.pixel_size_um
    bwd = float(np.hypot(*cols[f"{name}-"])) * cal.pixel_size_um
    value = fwd if direction > 0 else bwd if direction < 0 else (fwd + bwd) / 2.0
    return value if value > 0 else None


def axis_geometry(cols: dict[str, np.ndarray], pixel_size_um: float = 0.0) -> dict:
    """Human-readable summary: step size, image direction, asymmetry, crosstalk.

    The table is measured in image pixels (that is what the stabiliser closes on),
    but a step size means nothing to a person in pixels -- pass the camera's
    `pixel_size_um` and every step size is reported in MICROMETRES as well.
    """
    out = {}
    for axis in ("X", "Y"):
        fwd, bwd = cols[f"{axis}+"], cols[f"{axis}-"]
        out[axis] = {
            "px_per_step_fwd": float(np.hypot(*fwd)),
            "px_per_step_bwd": float(np.hypot(*bwd)),
            "um_per_step_fwd": float(np.hypot(*fwd)) * pixel_size_um or None,
            "um_per_step_bwd": float(np.hypot(*bwd)) * pixel_size_um or None,
            "asymmetry": float(np.hypot(*fwd) / np.hypot(*bwd)) if np.hypot(*bwd) else None,
            # direction a + move sends the image: 0 = right, +90 = down (image y is down)
            "image_dir_deg": float(np.degrees(np.arctan2(fwd[1], fwd[0]))),
        }
    ang = np.degrees(np.arctan2(cols["Y+"][1], cols["Y+"][0])
                     - np.arctan2(cols["X+"][1], cols["X+"][0]))
    ang = (ang + 180.0) % 360.0 - 180.0
    out["xy_angle_deg"] = float(ang)                        # +-90 for orthogonal axes
    out["non_orthogonality_deg"] = float(abs(abs(ang) - 90.0))
    return out
