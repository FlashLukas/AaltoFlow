"""The hardware interface -- what the brain is allowed to assume about the card.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a DaqBackend, whether it is the real NI USB-6001 (nidaq.py)
or the simulator (sim.py). The brain (daq.py) depends ONLY on this, so swapping
real hardware for the simulator changes nothing above this line.

The LAYOUT (which inputs exist, which lines are inputs or outputs) is handed to
open() once: on the real card it decides which DAQmx tasks are created, and a
task cannot change what it is while it runs. That is why a direction change
needs a service restart.

Clamping to safety limits, refusing a write to an input line, and every
"what applies when" decision are the BRAIN's job, not the backend's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Layout:
    """What the card is set up as, fixed at service start.

    ai            indices (0..7) of the enabled analog inputs, in read order
    ai_terminal   the terminal configuration of each of those, same order
    di / do       indices (0..12) of the digital lines that are inputs / outputs
                  (into config.DIO_LINES); an "unused" line is in neither
    """

    ai: tuple = ()
    ai_terminal: tuple = ()
    di: tuple = ()
    do: tuple = ()
    directions: tuple = field(default=())   # 13 strings: in / out / unused


@runtime_checkable
class DaqBackend(Protocol):
    def open(self, layout: Layout) -> None:
        """Claim the card (real backend only) and create the tasks for `layout`.
        Must NOT write any AO or DO value: the brain decides what is written."""

    def close(self) -> None:
        """Close the tasks and release the claim. Writes nothing: a safe state
        on shutdown is the brain's job (and only if the config asks for one)."""

    def idn(self) -> str:
        """A one-line identification ("NI USB-6001 SN 1A2B3C4 (Dev1)")."""

    def read_ai(self, samples: int, rate_Hz: float) -> list:
        """One hardware-timed acquisition of every enabled input: `samples`
        samples per channel at `rate_Hz`, returned as the MEAN volts per
        channel, in layout.ai order."""

    def write_ao(self, channel: int, volts: float) -> None:
        """Drive ao<channel> to `volts` (already clamped by the brain)."""

    def read_di(self) -> dict:
        """{line index: bool} for every input line."""

    def write_do(self, line: int, level: bool) -> None:
        """Drive an OUTPUT line (the brain has checked it is one)."""

    def read_do(self) -> dict:
        """{line index: bool or None} -- the level each output line is DRIVEN
        at, if the card can tell; None where it cannot."""
