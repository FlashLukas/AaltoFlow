"""The hardware interface the brain is allowed to call (§3 of the guide).

This is a ``typing.Protocol`` -- a *structural* interface.  Any object that has
these methods (the simulator OR the real d-Drive driver) is a valid backend;
there is no base class to inherit.  The brain depends ONLY on this Protocol, so
the two implementations are perfectly interchangeable.

Axis is always an integer index: 0=X, 1=Y.  Positions are in micrometres (um)
and slew rate in um/s.

Contract notes
--------------
* ``set_setpoint`` commands a target and returns immediately.  How fast the
  stage actually gets there depends on the loop mode and the slew rate; the
  brain observes progress by polling ``read_position``.
* ``read_position`` returns the MEASURED position: the strain-gauge reading in
  closed loop, or the drive-derived estimate in open loop.
* Loop switching and slew-rate limiting are per axis.  Clamping, ramping and
  sequencing are the brain's job -- the backend just does what it's told.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PiezoBackend(Protocol):
    # -- connection -------------------------------------------------------- #
    def open(self) -> None: ...
    def close(self) -> None: ...
    def idn(self) -> str: ...

    # -- position (fire-and-forget setpoint, polled read) ------------------ #
    def set_setpoint(self, axis: int, position: float) -> None: ...
    def read_position(self, axis: int) -> float: ...

    # -- loop mode --------------------------------------------------------- #
    def set_closed_loop(self, axis: int, enabled: bool) -> None: ...
    def get_closed_loop(self, axis: int) -> bool: ...

    # -- native velocity limiting (controller slew rate) ------------------- #
    # rate in um/s; 0 means "no limit / as fast as possible".
    def set_slew_rate(self, axis: int, rate: float) -> None: ...
    def read_slew_rate(self, axis: int) -> float: ...
