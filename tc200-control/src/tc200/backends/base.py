"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a HeaterBackend. There are two: the simulator (`sim.py`) and
the real TC200 over its USB serial port (`serial_tc200.py`). The Heater brain
depends ONLY on this interface.

The methods mirror the TC200's own command set one to one (manual, section
6.3.2), including its one awkward verb: `ens` TOGGLES the output -- there is no
"enable" or "disable", only "flip". So the backend offers `toggle_enable()`
and `read_status()`, and the BRAIN does read-then-toggle-then-confirm, in one
place, for both backends.

Everything is fire-and-forget, like the instrument: `set_setpoint` stores a
new target and returns; the controller's own PID then heats towards it and the
brain watches the readings to see it arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class StatusBits:
    """The TC200's status byte (stat?), decoded. Manual, "The Status Byte":

        bit 0  1 = output enabled
        bit 1  1 = CYCLE mode (0 = NORMAL)
        bit 2/3  sensor select (PTC100 / PTC1000; neither = TH10K)
        bit 4/5  display unit select
        bit 6  1 = sensor alarm (open or shorted sensor)
        bit 7  1 = cycle paused
    plus the text "TMAX ERROR" when the over-temperature alarm is latched.
    """

    enabled: bool
    cycle_mode: bool = False
    sensor_alarm: bool = False
    tmax_alarm: bool = False
    raw: int = 0


@runtime_checkable
class HeaterBackend(Protocol):
    """One TC200: one heater output, one sensor input."""

    simulated: bool

    def open(self) -> None:
        """Connect. Must NOT change anything on the controller: the brain
        adopts what the box is already doing."""

    def close(self) -> None:
        """Disconnect. Must NOT change anything either (switching the heater
        off at shutdown is the BRAIN's decision). Safe to call twice."""

    def idn(self) -> str:
        """Identification string, '' if unknown."""

    # ---- temperature -------------------------------------------------------
    def read_temperature(self) -> float:
        """TEMP ACTUAL in degC (tact?)."""

    def read_setpoint(self) -> float:
        """TEMP SET in degC (tset?)."""

    def set_setpoint(self, temperature_C: float) -> None:
        """tset=... Returns at once; the controller heats on its own."""

    # ---- output ------------------------------------------------------------
    def read_status(self) -> StatusBits:
        """The decoded status byte (stat?)."""

    def toggle_enable(self) -> None:
        """ens: flip the output between enabled and disabled."""

    # ---- stored settings ---------------------------------------------------
    def read_sensor(self) -> str:
        """'ptc100' | 'ptc1000' | 'th10k' (sns?)."""

    def set_sensor(self, sensor: str) -> None:
        """sns=..."""

    def read_pid(self) -> tuple[int, int, int]:
        """(P, I, D) gains (pid?)."""

    def set_p_gain(self, p: int) -> None: ...
    def set_i_gain(self, i: int) -> None: ...
    def set_d_gain(self, d: int) -> None: ...

    def read_pmax(self) -> float:
        """Output power ceiling in W."""

    def set_pmax(self, watts: float) -> None: ...

    def read_tmax(self) -> float:
        """Over-temperature trip in degC."""

    def set_tmax(self, temperature_C: float) -> None: ...
