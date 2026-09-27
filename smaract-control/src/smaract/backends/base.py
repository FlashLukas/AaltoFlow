"""The hardware interface the brain is allowed to call (section 3 of the guide).

This is a ``typing.Protocol`` -- a *structural* interface. Any object that has
these methods (the simulator OR the real SCU driver) is a valid backend; there
is no base class to inherit. The brain depends ONLY on this Protocol.

Units at this boundary are PHYSICAL: mm for positions, Hz for the step
frequency, ms for hold times. Encoder counts, the sign convention and the DLL's
integer types stay inside the real backend.

Fire-and-forget: ``move_absolute``, ``move_relative`` and ``find_reference``
START a motion and return at once. Progress is observed by polling
``channel_state`` and ``read_position_mm``. Clamping and "is this allowed" are
the brain's job; a backend commands what it is told.

``channel_state`` returns one of the SCU channel states, as lowercase names:

    "stopped"             idle, not holding
    "setting_amplitude"   (open-loop housekeeping)
    "moving"              open-loop stepping
    "targeting"           closed-loop move towards a target
    "holding"             closed loop actively holding the target
    "calibrating"         sensor calibration
    "moving_to_reference" searching the reference marks
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: The states that mean "the carriage is (or may be) in motion".
BUSY_STATES = ("setting_amplitude", "moving", "targeting", "calibrating",
               "moving_to_reference")

#: Every state name a backend may report, in the SCU's numeric order.
CHANNEL_STATES = ("stopped", "setting_amplitude", "moving", "targeting",
                  "holding", "calibrating", "moving_to_reference")


@runtime_checkable
class SmaractBackend(Protocol):
    # -- connection -------------------------------------------------------- #
    def open(self) -> None: ...
    def close(self) -> None: ...
    def idn(self) -> str: ...
    def sensor_present(self) -> bool: ...

    # -- motion (fire-and-forget) ----------------------------------------- #
    def move_absolute(self, position_mm: float, hold_ms: int) -> None: ...
    def move_relative(self, delta_mm: float, hold_ms: int) -> None: ...
    def find_reference(self, hold_ms: int) -> None: ...
    def stop(self) -> None: ...

    # -- read-back --------------------------------------------------------- #
    def read_position_mm(self) -> float: ...
    def channel_state(self) -> str: ...
    def physical_position_known(self) -> bool: ...

    # -- speed ------------------------------------------------------------- #
    def set_max_frequency(self, hz: int) -> None: ...
    def get_max_frequency(self) -> int: ...
