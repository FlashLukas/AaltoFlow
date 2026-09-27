"""The hardware interface -- what the rest of the code is allowed to assume.

`typing.Protocol` is Python's "structural interface": any object that has these
methods counts as a MonochromatorBackend, whether it is the real Cornerstone 260
or the simulator. The brain depends ONLY on this, so swapping real hardware for
the simulator changes nothing above this line.

Two kinds of call:
  * START calls (goto, set_grating, set_filter, set_port, step) begin a
    mechanical move and RETURN AT ONCE. The instrument takes seconds to slew or
    to swap gratings; blocking here would freeze whoever called.
  * `read_state()` reports where everything is AND whether a move is still in
    progress (`moving`). The brain's worker thread calls it periodically and
    is the only place that decides "arrived".

Clamping to the safety limits is the brain's job, not the backend's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class MonoState:
    """One reading of the instrument."""

    wavelength_nm: float
    grating: int
    shutter_open: bool
    filter: int = 0              # 0 = no wheel / out of position (manual, FILTER?)
    port: int = 1                # 1 = axial, 2 = lateral
    step_position: int = 0       # STEP? -- motor steps from the home sensor
    moving: bool = False
    error_code: int | None = None  # ERROR? after STB? reported an error, else None


@runtime_checkable
class MonochromatorBackend(Protocol):
    """A motorised grating monochromator (the Oriel Cornerstone 260)."""

    simulated: bool

    def open(self) -> None:
        """Connect and initialise (units nm). Must NOT move anything."""

    def close(self) -> None:
        """Disconnect. Safe to call on shutdown/crash."""

    def idn(self) -> str:
        """Identification string (INFO?). '' if unknown."""

    def grating_info(self, n: int) -> tuple[int, str] | None:
        """(lines/mm, label) stored in the instrument for grating n, or None."""

    def read_state(self) -> MonoState:
        """Everything the status shows, plus whether a move is in progress."""

    # ---- start a move (return at once) ------------------------------------
    def goto(self, nm: float) -> None:
        """GOWAVE: slew to the step closest to `nm`."""

    def set_grating(self, n: int) -> None:
        """GRAT n: swap gratings (parks at grating 1's max or at zero order)."""

    def set_filter(self, n: int) -> None:
        """FILTER n: move the filter wheel."""

    def set_port(self, n: int) -> None:
        """OUTPORT n: flip the exit mirror (1 axial, 2 lateral)."""

    def step(self, n: int) -> None:
        """STEP n: move the drive by n motor steps (+ = longer wavelength)."""

    # ---- instantaneous ----------------------------------------------------
    def set_shutter(self, open_: bool) -> None:
        """SHUTTER O / SHUTTER C."""

    def abort(self) -> None:
        """ABORT: stop any wavelength motion immediately."""

    def calibrate(self, nm: float) -> None:
        """CALIBRATE: declare the CURRENT position to be `nm` (stored offset)."""
