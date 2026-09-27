"""The hardware interface the brain is allowed to call (section 3 of the guide).

This is a ``typing.Protocol`` -- a *structural* interface.  Any object with
these methods (the simulator OR the real serial driver) is a valid backend;
there is no base class to inherit.  The brain depends ONLY on this Protocol, so
the two implementations are interchangeable.

Every call names a mount by its BUS ADDRESS, a single hex digit "0".."F": the
Elliptec bus is shared, and the address is what the wire protocol itself uses.
Angles here are DEVICE degrees (the mount's own encoder frame); the user
offset and the clamping are the brain's business, not the backend's.

Contract (fire-and-forget): ``start_home`` / ``start_move_*`` START a motion
and return at once.  Progress is observed with ``poll``, which the brain's
single worker thread calls a few ten times a second.  The backend is only ever
called from that one thread, so it needs no locking of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Elliptec status codes (the two hex digits of a "GS" reply), in the words of
#: the Thorlabs ELLx protocol manual.  0 = OK and 9 = busy are not errors.
STATUS_TEXT = {
    0: "OK",
    1: "communication timeout",
    2: "mechanical timeout",
    3: "command error / not supported",
    4: "value out of range",
    5: "module isolated",
    6: "module out of isolation",
    7: "initialisation error",
    8: "thermal error",
    9: "busy",
    10: "sensor error",
    11: "motor error",
    12: "out of range",
    13: "over current error",
}


def status_text(code: int) -> str:
    return STATUS_TEXT.get(int(code), f"unknown status {code}")


@dataclass
class AxisReading:
    """What one ``poll`` of one mount tells the brain."""

    device_deg: float   # encoder angle, degrees (may exceed 360 after relative moves)
    moving: bool        # a commanded motion has not finished yet
    error_code: int     # last Elliptec status code (0 = OK)


@runtime_checkable
class ElliptecBackend(Protocol):
    # -- connection -------------------------------------------------------- #
    # open() may only QUERY the mounts (adopt-on-start rule, 2026-09-27): it
    # must not change the speed, the angle or anything else.
    def open(self, addresses: list) -> None: ...
    def close(self) -> None: ...
    def idn(self) -> str: ...
    def device_info(self, address: str) -> dict: ...

    # -- motion (fire-and-forget) ----------------------------------------- #
    def start_home(self, address: str, ccw: bool = False) -> None: ...
    def start_move_abs(self, address: str, device_deg: float) -> None: ...
    def start_move_rel(self, address: str, delta_deg: float) -> None: ...
    def stop(self, address: str) -> None: ...
    def poll(self, address: str) -> AxisReading: ...

    # -- parameters -------------------------------------------------------- #
    def set_velocity(self, address: str, percent: int) -> None: ...
    def read_velocity(self, address: str) -> "int | None": ...   # None = not known
