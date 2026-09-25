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
