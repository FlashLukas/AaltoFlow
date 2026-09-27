"""The hardware interface the brain is allowed to call (section 3 of the guide).

A ``typing.Protocol`` -- a *structural* interface. Any object with these methods
(the simulator OR the real AG-UC2 driver) is a valid backend; there is no base
class to inherit.

It is a thin mirror of the AG-UC2 command set, deliberately: everything is in
the controller's NATIVE language -- STEPS, amplitudes, the controller's own
axis numbers 1/2 -- and every method is one (or, for setters, one + an error
check) ASCII command. Micrometres, clamping, leash and bookkeeping are the
brain's job.

    method              AG-UC2 command
    ------------------  --------------------------------------------------
    move_by(ax, n)      axPRn      relative move of n steps
    jog(ax, mode)       axJAmode   continuous move, mode -4..4 (0 = stop jog)
    stop(ax)            axST       stop, axis goes to READY
    read_position(ax)   axTP       step counter (forward minus backward)
    axis_state(ax)      axTS       0 ready, 1 stepping (PR), 2 jogging (JA),
                                   3 moving to limit (MV/MA/PA)
    zero_counter(ax)    axZP       step counter := 0
    set_amplitude(...)  axSU+n / axSU-n   step amplitude 1..50 per direction
    read_amplitude(...) axSU+? / axSU-?
    limit_status()      PH         bit 0 = axis 1 limit, bit 1 = axis 2 limit
    enable_remote()     MR (+CC)   remote mode: the ONLY way to read TP/SU/PH
    move_to_limit(ax,m) axMVm      jog to the limit switch and stop there
    measure_position(ax) axMA      BLOCKING: position in 1/1000 of the travel
    move_absolute(ax,p) axPAp      BLOCKING: go to p/1000 of the travel

Contract notes:

* ``open()`` only opens the link and reads the firmware version. It must not
  change anything on the controller (AaltoFlow rule 2026-09-27: read the
  instrument state at start, never change it). The brain decides, after
  looking at the axis states (TS works in local mode), when to call
  ``enable_remote()`` -- which the manual refuses while an axis moves.
* ``move_by``, ``jog`` and ``move_to_limit`` START motion and return
  immediately (fire-and-forget); progress is observed by polling
  ``axis_state`` and ``read_position``.
* ``measure_position`` / ``move_absolute`` (MA / PA) work only on stages
  WITH a limit switch (AG-LS25). The controller interrupts the USB link while
  they run (up to 2 minutes), so these two calls BLOCK until the controller
  answers. The brain runs them in a routine thread and never lets the poll or
  a command wait behind them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: TS codes (manual, TS command).
READY, STEPPING, JOGGING, MOVING_TO_LIMIT = 0, 1, 2, 3


@runtime_checkable
class AgilisBackend(Protocol):
    # -- connection -------------------------------------------------------- #
    def open(self) -> None: ...
    def close(self) -> None: ...
    def idn(self) -> str: ...

    # -- motion (fire-and-forget, STEPS, controller axis 1/2) -------------- #
    def move_by(self, hw_axis: int, delta_steps: int) -> None: ...
    def jog(self, hw_axis: int, mode: int) -> None: ...
    def stop(self, hw_axis: int) -> None: ...
    def read_position(self, hw_axis: int) -> int: ...
    def axis_state(self, hw_axis: int) -> int: ...
    def zero_counter(self, hw_axis: int) -> None: ...

    # -- drive ------------------------------------------------------------- #
    def set_amplitude(self, hw_axis: int, direction: int, amplitude: int) -> None: ...
    def read_amplitude(self, hw_axis: int, direction: int) -> int: ...
    def limit_status(self) -> int: ...

    # -- start-up and limit-switch stages (AG-LS25) ------------------------ #
    def enable_remote(self) -> None: ...
    def move_to_limit(self, hw_axis: int, mode: int) -> None: ...
    def measure_position(self, hw_axis: int) -> int: ...
    def move_absolute(self, hw_axis: int, permille: int) -> int: ...
