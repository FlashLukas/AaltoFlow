"""Simulated hardware: a fake SMB100A.

It implements the RFSource interface from `base`, so the Generator cannot tell
it apart from the real instrument. The point is to develop and test everything
-- limits, the service, the client, the console -- with nothing plugged in.

The "physics" is deliberately trivial: a signal generator just remembers what
you told it. The only lifelike touches are (1) it reports RF OFF until opened,
and (2) `read_*` returns the last commanded value, exactly like querying the
real box right after a set.
"""

from __future__ import annotations

from ..config import Signal


class SimulatedSMB100A:
    """Pretends to be an R&S SMB100A RF signal generator."""

    def __init__(self, startup: Signal | None = None):
        s = startup or Signal()
        # remembered instrument state
        self._freq = float(s.frequency_Hz)
        self._power = float(s.power_dBm)
        self._phase = float(s.phase_deg)
        self._output = False          # a real box powers up with RF off
        self._open = False

    def open(self) -> None:
        self._open = True
        self._output = False          # mirror OUTP OFF on connect/init

    def close(self) -> None:
        self._output = False          # RF off on the way out
        self._open = False

    # ---- RF output on/off ------------------------------------------------
    def set_output(self, on: bool) -> None:
        self._output = bool(on)

    def read_output(self) -> bool:
        return self._output

    # ---- level -----------------------------------------------------------
    def set_power(self, dBm: float) -> None:
        self._power = float(dBm)

    def read_power(self) -> float:
        return self._power

    # ---- frequency -------------------------------------------------------
    def set_frequency(self, hz: float) -> None:
        self._freq = float(hz)

    def read_frequency(self) -> float:
        return self._freq

    # ---- phase -----------------------------------------------------------
    def set_phase(self, deg: float) -> None:
        self._phase = float(deg)

    def read_phase(self) -> float:
        return self._phase

    # ---- identity --------------------------------------------------------
    def idn(self) -> str:
        return "Rohde&Schwarz,SMB100A,SIMULATED,0.0" if self._open else ""
