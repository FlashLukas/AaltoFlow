"""The hardware interface the brain is allowed to call (section 3 of the guide).

A ``typing.Protocol`` -- a *structural* interface: any object with these
methods (the simulator OR the real Kinesis driver) is a valid backend, there is
no base class to inherit. The brain depends ONLY on this.

One axis. Positions are CONTROLLER degrees (continuous over many turns),
velocities deg/s, accelerations deg/s^2.

Fire-and-forget: ``move_to``, ``home`` and ``stop`` START something and return
at once; completion is observed by polling ``is_moving`` / ``is_homed``.
Clamping, wrap policy and sequencing are the brain's job -- a backend commands
exactly what it is told.

Thread safety is ALSO the brain's job: it calls every method below under one
lock, so a backend never sees two calls at once.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RotatorBackend(Protocol):
    # -- connection -------------------------------------------------------- #
    def open(self) -> None: ...
    def close(self) -> None: ...
    def idn(self) -> str: ...

    # -- motion (fire-and-forget) ----------------------------------------- #
    def home(self) -> None: ...
    def is_homed(self) -> bool: ...
    def move_to(self, position: float) -> None: ...
    def is_moving(self) -> bool: ...
    def stop(self, immediate: bool = False) -> None: ...
    def read_position(self) -> float: ...

    # -- motion parameters ------------------------------------------------- #
    def set_velocity(self, velocity: float) -> None: ...
    def set_acceleration(self, acceleration: float) -> None: ...
    def read_velocity_params(self) -> tuple[float, float]: ...   # (vel, acc)
