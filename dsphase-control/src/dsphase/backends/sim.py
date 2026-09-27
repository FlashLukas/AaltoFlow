"""Simulated hardware: a fake PS6000L phase shifter.

It implements PhaseShifterBackend, so the brain cannot tell it apart from the
real unit. What it models, and why:

* the phase is quantized to the device step and wrapped into -180..+180, as
  the unit itself would do -- so if the brain ever forgot to round, the readback
  would disagree with the request and a test would catch it;
* the attenuator is quantized to its 0.25 dB step, 0..30 dB;
* the output powers up OFF and is switched off on close;
* an "ideal" phase: no frequency-dependent error. The datasheet error band is
  reported separately (phasemath.datasheet_accuracy_deg), not faked here.

`fail_next_read` lets a test simulate a USB hiccup.
"""

from __future__ import annotations

from ..config import Device
from ..phasemath import quantize, wrap


class SimulatedPS6000L:
    """Pretends to be a DS Instruments PS6000L."""

    def __init__(self, device: Device | None = None):
        self.device = device or Device()
        self._phase = 0.0
        self._att = 0.0
        self._output = False           # a real box powers up with RF off
        self._freq = None              # last carrier told to us, if any
        self._open = False
        self.writes = 0                # how many hardware writes (tests use it)
        self.fail_next_read = False

    def open(self) -> None:
        self._open = True
        self._output = False           # mirror OUTP:STAT OFF on connect

    def close(self) -> None:
        self._output = False           # RF off on the way out
        self._open = False

    def _check_read(self):
        if not self._open:
            raise RuntimeError("not open")
        if self.fail_next_read:
            self.fail_next_read = False
            raise IOError("simulated USB read timeout")

    # ---- phase -----------------------------------------------------------
    def set_phase(self, deg: float) -> None:
        self.writes += 1
        self._phase = wrap(quantize(float(deg), self.device.phase_step_deg))

    def read_phase(self) -> float:
        self._check_read()
        return self._phase

    # ---- attenuator ------------------------------------------------------
    def set_attenuation(self, dB: float) -> None:
        self.writes += 1
        v = quantize(float(dB), self.device.att_step_dB)
        self._att = min(30.0, max(0.0, v))

    def read_attenuation(self) -> float:
        self._check_read()
        return self._att

    # ---- output ----------------------------------------------------------
    def set_output(self, on: bool) -> None:
        self.writes += 1
        self._output = bool(on)

    def read_output(self) -> bool:
        self._check_read()
        return self._output

    # ---- frequency -------------------------------------------------------
    def set_frequency(self, mhz: float) -> None:
        self._freq = float(mhz)

    # ---- identity --------------------------------------------------------
    def idn(self) -> str:
        return "DS Instruments,PS6000L,SIMULATED,0.0" if self._open else ""
