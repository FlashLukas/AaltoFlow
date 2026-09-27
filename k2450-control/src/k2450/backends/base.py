"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as a SourceMeterBackend, whether it is the real Keithley
2450 or the simulator. The SourceMeter brain depends ONLY on this interface, so
swapping real hardware for the simulator changes nothing above this line.

Vocabulary used throughout:
    fn   -- the SOURCE function, "voltage" or "current".
    mfn  -- the MEASURE function, always the other one (source V -> measure I).

Backends do no clamping and no safety logic: that is the brain's job. They
just translate one call into the instrument's commands. Every setter is a plain
write; the brain holds a lock so only one thread talks to the instrument.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

FUNCS = ("voltage", "current")

#: The 2450's source AND measure ranges (reference manual, "Source and measure
#: ranges"). A range reaches 105 % of its nominal value.
V_RANGES = (0.02, 0.2, 2.0, 20.0, 200.0)
I_RANGES = (1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0)
OVERRANGE = 1.05

#: What the 2450 itself can do (datasheet). The user envelope in config
#: [limits] may be NARROWER than this but never wider: a .ini or a set_config
#: that asks for 500 V is cut back to these, because the instrument would
#: refuse the command and the brain would then report a setpoint that is not
#: applied.
V_MAX = 210.0          # |V| of the high-voltage box
I_MAX = 1.05           # |I| of the high-current box
BOX_V = 21.0           # |V| of the high-current box
BOX_I = 0.105          # |I| of the high-voltage box
NPLC_MIN = 0.01
NPLC_MAX = 10.0


def other(fn: str) -> str:
    """The measured quantity for a source function."""
    return "current" if fn == "voltage" else "voltage"


def range_table(fn: str) -> tuple[float, ...]:
    return V_RANGES if fn == "voltage" else I_RANGES


def snap_range(fn: str, value: float) -> float:
    """The smallest range that holds |value| -- what the instrument does when
    you ask for a range that is not one of its own (it snaps UP)."""
    table = range_table(fn)
    v = abs(float(value))
    for r in table:
        if r >= v * (1 - 1e-9):
            return r
    return table[-1]


def auto_range_for(fn: str, value: float) -> float:
    """The range autorange would pick for a reading of `value` (sim helper)."""
    table = range_table(fn)
    v = abs(float(value))
    for r in table:
        if r * OVERRANGE >= v:
            return r
    return table[-1]


@dataclass
class Reading:
    """One measurement, as the instrument reported it.

    measured  -- the measure function's value (A when sourcing V, V when sourcing I);
                 NaN when the reading overflowed the measure range.
    source    -- the source READBACK: what the SMU actually applied, measured.
                 In compliance this is NOT the setpoint (a 10 V source into a
                 short at a 1 mA limit sits at a few mV).
    tripped   -- True when the compliance limit was reached ("in compliance").
    overflow  -- the measured value exceeded 105 % of a fixed measure range.
    measure_range / source_range -- the ranges in use for this reading
                 (autorange moves them; NaN if the backend cannot tell).
    """

    measured: float
    source: float
    tripped: bool = False
    overflow: bool = False
    measure_range: float = float("nan")
    source_range: float = float("nan")


@runtime_checkable
class SourceMeterBackend(Protocol):
    """A source-measure unit (the Keithley 2450)."""

    def open(self) -> None:
        """Connect and initialise. MUST leave the output OFF."""

    def close(self) -> None:
        """Output off and disconnect. Safe to call on shutdown/crash, twice."""

    def idn(self) -> str:
        """Instrument identification string (*IDN?). '' if unknown."""

    # ---- source ------------------------------------------------------------
    def set_source_function(self, fn: str) -> None:
        """Source `fn` and measure the other quantity."""

    def set_limit(self, fn: str, value: float) -> None:
        """Compliance for source function fn: ILIM (A) when fn is voltage,
        VLIM (V) when fn is current."""

    def set_level(self, fn: str, value: float) -> None:
        """The source level, V or A."""

    def set_source_range(self, fn: str, auto: bool, value: float) -> None:
        """Source range: autorange, or a fixed range (instrument snaps up)."""

    def get_source_range(self, fn: str) -> float:
        """The source range in use."""

    # ---- measure -----------------------------------------------------------
    def set_measure_range(self, mfn: str, auto: bool, value: float) -> None:
        """Measure range of the measured quantity: auto, or fixed."""

    def get_measure_range(self, mfn: str) -> float:
        """The measure range in use."""

    def set_nplc(self, mfn: str, nplc: float) -> None:
        """Integration time in power-line cycles."""

    def set_four_wire(self, on: bool) -> None:
        """Remote sense (4-wire) on/off."""

    # ---- output and readings -------------------------------------------------
    def set_output(self, on: bool) -> None:
        """Output relay on/off."""

    def get_output(self) -> bool:
        """True when the output is on."""

    def measure(self) -> Reading:
        """Take ONE new reading (blocks for about NPLC / line frequency)."""
