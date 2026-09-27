"""Simulated hardware: a fake HP 8648D.

It implements the SigGenBackend interface from `base`, so the brain cannot tell
it apart from the real instrument. The point is to develop and test everything
-- limits, the service, the client, the console, the GUI -- with nothing
plugged in.

What it models, because each one changes what the rest of the code must do:

* *RST state at open: RF off, 100 MHz, -136 dBm, all modulation off.
* the instrument's RESOLUTION: frequency kept to 10 Hz, level to 0.1 dB, so a
  read-back differs slightly from what was asked (the settle tolerance in
  `describe` has to cope with that, exactly as on the real box).
* the UNSPECIFIED-LEVEL flag: a level above the specified maximum at the
  current frequency is accepted, but flagged in the POWer condition register.
* REVERSE-POWER PROTECTION: `inject_reverse_power()` stands in for a signal
  (an amplifier, a reflection) arriving at the RF output. The box then turns
  the RF off and latches the RPP bit until the output is switched on again,
  which is also how the real one is re-armed.
* modulation: nothing switches it on here, but the state is kept so the
  "all modulation off" check has something to read.
* an error queue, like SYST:ERR?.
"""

from __future__ import annotations

from .. import spec
from .base import RPP_BIT, UNSPECIFIED_BIT


class SimulatedHP8648:
    """Pretends to be an HP 8648D RF signal generator."""

    def __init__(self, hardware=None):
        # A reference to the config's Hardware group, read LIVE: set_config
        # edits the config in place, so switching option_1ea in Settings is
        # seen here at once, exactly like fitting the option would be.
        from ..config import Hardware
        self._hw = hardware if hardware is not None else Hardware()
        self._open = False
        self._reset()
        self._errors: list[str] = []

    def _reset(self):
        """The *RST state (Operation and Service Guide, 'Receiving the Clear
        Message' table)."""
        self._freq = 100e6
        self._power = -136.0
        self._output = False
        self._mod = {"am": False, "fm": False, "pm": False}
        self._rpp = False

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        self._open = True
        self._reset()

    def close(self) -> None:
        self._output = False
        self._open = False

    # ---- output ----------------------------------------------------------

    def set_output(self, on: bool) -> None:
        self._output = bool(on)
        if on:
            # The manual: after removing the reverse-power source, "turn the
            # RF power on again" -- that is the reset.
            self._rpp = False

    def read_output(self) -> bool:
        return self._output

    # ---- level / frequency ------------------------------------------------

    def set_power(self, dBm: float) -> None:
        lo = spec.POWER_MIN_DBM
        v = spec.quantize_power(float(dBm))
        if v < lo:
            self._errors.append('-222,"Data out of range"')
            v = lo
        self._power = v

    def read_power(self) -> float:
        return self._power

    def set_frequency(self, hz: float) -> None:
        v = spec.quantize_frequency(float(hz))
        if not spec.FREQ_MIN_HZ <= v <= spec.FREQ_MAX_HZ:
            self._errors.append('-222,"Data out of range"')
            v = min(max(v, spec.FREQ_MIN_HZ), spec.FREQ_MAX_HZ)
        self._freq = v

    def read_frequency(self) -> float:
        return self._freq

    # ---- status registers -----------------------------------------------

    def read_power_condition(self) -> int:
        cond = 0
        if self._rpp:
            cond |= RPP_BIT
        if self._power > spec.spec_max_dBm(self._freq, bool(self._hw.option_1ea)) + 1e-9:
            cond |= UNSPECIFIED_BIT
        return cond

    def read_modulation(self) -> dict:
        return dict(self._mod)

    def modulation_off(self) -> None:
        for k in self._mod:
            self._mod[k] = False

    def drain_errors(self) -> list[str]:
        out, self._errors = self._errors, []
        return out

    def idn(self) -> str:
        return "HEWLETT-PACKARD,8648D,SIMULATED,0.0" if self._open else ""

    # ---- simulation-only hooks (tests, demos) -------------------------------

    def inject_reverse_power(self) -> None:
        """Pretend a signal arrived at the RF output: RPP trips, RF goes off."""
        self._rpp = True
        self._output = False

    def force_modulation(self, kind: str, on: bool = True) -> None:
        """Pretend someone switched a modulation on at the front panel."""
        self._mod[kind] = bool(on)
