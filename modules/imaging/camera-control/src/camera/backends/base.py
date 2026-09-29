"""The hardware interfaces the brain is allowed to call (blueprint §3).

These are ``typing.Protocol`` classes -- *structural* interfaces.  Any object with
the right methods is a valid backend; there is no base class to inherit.  The
brain depends ONLY on these Protocols, so the simulator and the real drivers are
perfectly interchangeable.

This module needs THREE backends because the camera brain is a coordinator:

  * CameraBackend -- the imaging device (grabs frames).
  * XYStage       -- the sample stage in X/Y, always another module's SERVICE:
                     kim-control on the lab rig (backends/remote_kim.py), or
                     piezo-control (backends/remote_xy.py); SimXYStage in the sim.
  * ZFocus        -- the focus axis: kim-control's Z on the lab rig, else
                     zpiezo-control or an own KCube.

Units: image data is a 2-D/3-D uint8 ndarray; stage positions are micrometres
(um); Z is in the Z device's own unit -- volts for a piezo, um for KIM.

OPTIONAL extras a backend may add (the brain checks with getattr, so the
Protocols stay as they are and existing backends need nothing):
  * ``owns_limits = True`` + ``xy_range()`` / ``z_range()`` -- clamp to the
    stage's LIVE range instead of cfg.limits (KIM: centred on the Datum, leash).
  * ``z_unit()`` -> "V" | "um" -- labels in status, GUI and describe.
  * ``open_loop = True`` -- autofocus approaches every Z target from below.
  * ``wait_settled()`` -- block until the last set_z has arrived.
  * A Z with a STEP COUNTER whose up and down steps differ (slip-stick; the
    camera's Z step calibration, Camera.calibrate_z_steps, 2026-09-28):
    ``counter_steps()`` / ``move_counter(steps)`` (raw counter, no conversion),
    ``step_sizes()`` -> (up, down) in Z units per step, and
    ``set_step_sizes(up, down)``. With two different sizes set, read_z/set_z
    work in a coordinate that steps by the size of the direction travelled
    (DirectionalCounter below), so going up 2 um and down 2 um comes back.
  * CameraBackend ``last_deep()`` -> (uint16 frame, bit depth) or None: the
    full-depth copy of the frame the last grab() returned (same buffer), for
    the spot-size metrics; grab() itself stays 8-bit (2026-09-28).
  * CameraBackend ``deep_note()`` -> str (optional, 2026-09-29): why the last
    grab() left NO deep frame (e.g. "the camera's PixelFormat is Mono8 ..."),
    "" when it did. The brain logs it once per start and per change.
See backends/remote_kim.py for the one backend that has them all.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class CameraBackend(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def idn(self) -> str: ...
    def grab(self) -> np.ndarray: ...

    # -- controllable camera parameters (GenICam-style feature model) ------ #
    # ``features()`` returns a list of descriptor dicts so the GUI can build
    # controls for ANY camera generically.  Each descriptor:
    #   {
    #     "name":     "ExposureTime",        # GenICam feature name (the key)
    #     "display":  "Exposure Time",       # human label
    #     "type":     "float"|"int"|"bool"|"enum"|"command"|"string",
    #     "value":    <current value>,        # absent for command
    #     "min":      <number or None>,       # float/int only
    #     "max":      <number or None>,
    #     "inc":      <step or None>,
    #     "unit":     "us" | "" ,
    #     "options":  [<enum entries>] or None,
    #     "writable": bool,
    #     "category": "Analog" | "AcquisitionControl" | ... (grouping hint)
    #   }
    def features(self) -> list: ...
    def get_feature(self, name: str): ...
    def set_feature(self, name: str, value) -> None: ...


@runtime_checkable
class XYStage(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def read_xy(self) -> tuple: ...          # (x_um, y_um) MEASURED
    def move_xy(self, x_um: float, y_um: float) -> None: ...  # fire-and-forget
    def moving(self) -> bool: ...


@runtime_checkable
class ZFocus(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def read_z(self) -> float: ...           # drive voltage, V
    def set_z(self, volts: float) -> None: ...
    def z_range(self) -> tuple: ...          # (min_v, max_v)


class DirectionalCounter:
    """A position from an open-loop step COUNTER when a step up is not a step down.

    The problem (kim Z, PIA25 slip-stick, rig 2026-09-28): the counter counts
    steps, but a step up moves the sample less than a step down. A position
    "steps x one step size" is then wrong after every reversal: go up 100
    steps and down 100 steps and the counter is back where it was while the
    sample is not. With the two step sizes known (Camera.calibrate_z_steps),
    the position is tracked MOVE BY MOVE instead: every move starts at an
    ANCHOR (counter, position) and adds (steps moved) x (the size of the
    direction it went). A move towards a target is planned the same way.

    Units: ``up``/``down``/``mean`` are position units per step (um on kim).
    A fresh anchor puts the position at steps x mean -- the stage's own
    reading -- so nothing jumps when the sizes are equal. Changing the sizes
    starts a new anchor there.

    Limit: a move made by SOMEONE ELSE (the kim GUI) between two of ours is
    counted as one move from the last anchor, in the direction the counter
    moved overall -- right for a single move, approximate for a back-and-forth.
    """

    def __init__(self) -> None:
        self._anchor = None      # (steps, position, (up, down, mean))

    def reset(self) -> None:
        self._anchor = None

    def position(self, steps: float, up: float, down: float, mean: float) -> float:
        sizes = (float(up), float(down), float(mean))
        a = self._anchor
        if a is None or a[2] != sizes:
            a = self._anchor = (float(steps), float(steps) * sizes[2], sizes)
        dn = float(steps) - a[0]
        return a[1] + dn * (sizes[0] if dn > 0 else sizes[1])

    def plan(self, steps_now: float, target: float, up: float, down: float,
             mean: float) -> float:
        """The counter value (not rounded) that puts the position at ``target``,
        re-anchoring at the current point so the move is one direction."""
        here = self.position(steps_now, up, down, mean)
        self._anchor = (float(steps_now), here, (float(up), float(down), float(mean)))
        d = float(target) - here
        size = float(up) if d > 0 else float(down)
        return float(steps_now) + (d / size if size > 0 else 0.0)
