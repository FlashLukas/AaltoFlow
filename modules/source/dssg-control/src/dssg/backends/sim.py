"""Simulated hardware: a fake DS Instruments SG12000L.

It implements the MicrowaveSource interface from `base`, so the brain cannot
tell it apart from the real instrument. The behaviour follows the SG series
datasheet (V3.6, Dec 2022) where it matters to a caller:

* The unit refuses (clamps) values outside ITS OWN range, like the firmware
  does, independently of the brain's safety envelope.
* Output power is set by a step attenuator in 0.5 dB steps, so the read-back
  power is the request ROUNDED to the step. A caller that asks for -7.3 dBm
  reads back -7.5 -- exactly why the scan's echo tolerance is half a step.
* The box starts in whatever state `Sim.state_*` describes (as if left like
  that from the front panel); `open()` only connects, it changes nothing, so
  the brain's adopt-on-start logic is exercised against a non-default state.
* The VERNIER (fine power trim, integer counts) shifts the simulated OUTPUT
  level by 0.05 dB per count. That number is an ASSUMPTION for the simulator
  only -- the vendor documents no dB per count. POWER? keeps reporting the
  attenuator setting (the vernier is a separate knob), so a power scan's echo
  is not disturbed by a vernier offset.
* The USB supply sags a little when the RF chain is on (the 12 GHz model draws
  ~0.8 A from USB), which makes the "USB volts" indicator come alive.
* An external 10 MHz reference is only "detected" if the Sim config says a
  cable is plugged in; choosing "auto" without one falls back to internal,
  the documented power-on behaviour.
"""

from __future__ import annotations

import random

from ..config import REFERENCES, Sim


class SimulatedSG12000L:
    """Pretends to be a DS Instruments SG12000L microwave signal generator."""

    def __init__(self, sim: Sim | None = None, power_step_dB: float = 0.5):
        self._sim = sim = sim or Sim()
        self._step = float(power_step_dB) if power_step_dB > 0 else 0.0
        # The state the box is already in (a real one keeps its last settings
        # across power cycles and front-panel use). Clamped/quantised like the
        # firmware would hold it.
        lo, hi = self.freq_range()
        self._freq = min(max(float(sim.state_frequency_Hz), lo), hi)
        self._power = self._quantise(float(sim.state_power_dBm))
        self._phase = float(sim.state_phase_deg) if sim.has_phase else 0.0
        self._vernier = int(sim.state_vernier) if sim.has_vernier else 0
        self._ref = sim.state_reference if sim.state_reference in REFERENCES else "auto"
        self._output = bool(sim.state_rf_on)
        self._buzzer = True               # what a unit does out of the box
        self._display = True
        self._open = False
        self._rng = random.Random(12000)

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        self._open = True             # connect only: the state stays as it was

    def close(self, rf_off: bool = True) -> None:
        if rf_off:
            self._output = False      # RF off on the way out (normal shutdown)
        self._open = False

    def idn(self) -> str:
        return "DS Instruments,SG12000L,SIMULATED,sim" if self._open else ""

    # ---- capabilities ------------------------------------------------------

    def freq_range(self) -> tuple[float, float]:
        return float(self._sim.freq_min_Hz), float(self._sim.freq_max_Hz)

    def power_range(self) -> tuple[float, float]:
        return float(self._sim.power_min_dBm), float(self._sim.power_max_dBm)

    def has_phase(self) -> bool:
        return bool(self._sim.has_phase)

    def has_vernier(self) -> bool:
        return bool(self._sim.has_vernier)

    # ---- RF output -------------------------------------------------------

    def set_output(self, on: bool) -> None:
        self._output = bool(on)

    def read_output(self) -> bool:
        return self._output

    # ---- frequency -------------------------------------------------------

    def set_frequency(self, hz: float) -> None:
        lo, hi = self.freq_range()
        self._freq = min(max(float(hz), lo), hi)

    def read_frequency(self) -> float:
        return self._freq

    # ---- power -------------------------------------------------------------

    def _quantise(self, dBm: float) -> float:
        """Round to the attenuator step, then keep inside the unit's range."""
        lo, hi = self.power_range()
        v = min(max(dBm, lo), hi)
        if self._step:
            v = round(v / self._step) * self._step
        return float(min(max(v, lo), hi))

    def set_power(self, dBm: float) -> None:
        self._power = self._quantise(float(dBm))

    def read_power(self) -> float:
        return self._power

    # ---- phase -------------------------------------------------------------

    def set_phase(self, deg: float) -> None:
        if not self.has_phase():
            raise RuntimeError("this unit has no phase control")
        self._phase = float(deg)

    def read_phase(self) -> float:
        return self._phase

    # ---- vernier -------------------------------------------------------------

    #: dB of output level per vernier count. A plausible guess for the
    #: SIMULATOR ONLY; the real unit's value is not documented and has to be
    #: measured (e.g. with a spectrum analyser) before anyone relies on it.
    SIM_DB_PER_COUNT = 0.05

    def set_vernier(self, n: int) -> None:
        if not self.has_vernier():
            raise RuntimeError("this unit has no vernier control")
        self._vernier = int(n)

    def read_vernier(self) -> int:
        return self._vernier

    def output_dBm(self) -> float:
        """What would really come out of the SMA port: the attenuator setting
        plus the vernier trim (simulator model only, see SIM_DB_PER_COUNT)."""
        return self._power + self.SIM_DB_PER_COUNT * self._vernier

    # ---- reference ---------------------------------------------------------

    def set_reference(self, mode: str) -> None:
        if mode not in ("internal", "external", "auto"):
            raise ValueError(f"unknown reference {mode!r}")
        self._ref = mode

    def read_reference(self) -> str:
        return self._ref

    def external_ref_detected(self) -> bool:
        return bool(self._sim.external_ref_present)

    # ---- health --------------------------------------------------------------

    def usb_volts(self) -> float:
        sag = 0.12 if self._output else 0.0
        return round(5.08 - sag + self._rng.gauss(0.0, 0.01), 3)

    def errors(self) -> list[str]:
        return []

    # ---- front-panel preferences -------------------------------------------

    def set_buzzer(self, on: bool) -> None:
        self._buzzer = bool(on)

    def set_display(self, on: bool) -> None:
        self._display = bool(on)
