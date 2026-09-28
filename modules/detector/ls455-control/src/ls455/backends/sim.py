"""A simulated Lake Shore 455 with an axial Hall probe, so everything runs
with nothing plugged in.

Enough physics that the controls visibly DO something:

  * The probe sits in a field `field_mT` (default 42 mT, "a small magnet") that
    drifts slowly by 0.2 % -- the way an electromagnet warms up -- plus an
    AC ripple of `ac_rms_mT` that only the RMS mode sees.
  * An un-zeroed probe has an offset (`offset_mT`, 0.05 mT ~ Earth's field
    plus Hall-element misalignment) until you run Zero probe.
  * Noise depends on the RANGE and the DC resolution, scaled from the manual's
    RMS-noise table: a 3.5 T range cannot resolve a microtesla, and 5 digits
    (1 Hz bandwidth) is ~30x quieter than 3 digits (100 Hz).
  * The DC filter is modelled as first order with the manual's time constant
    (0.01 / 0.1 / 1 s at 3 / 4 / 5 digits), so after a field step the reading
    CREEPS to the new value (7 s to 0.1 % at 5 digits). That is why the brain
    waits `settle_s` before it counts readings for an acquisition.
  * A manual range below the field saturates and flags `overload`.
  * PEAK mode reports the DC field plus/minus the AC ripple's amplitude
    (sqrt 2 x its RMS), picked by the peak display setting.
  * The probe can be unplugged (`probe_present = False` -> flag "no probe")
    and swapped (`swap_probe("HST")`), to test that the module re-reads it.

The meter's STATE at construction is whatever you pass (mode, digits, range,
unit, relative ...): the module must ADOPT it at start, never overwrite it, so
tests start the sim from a deliberately non-default state and check that
status shows exactly that.

`read_field` does not sleep by default, so tests stay fast; pass
`realtime=True` for the real reading rate (30 rdg/s, 10 at 5 digits).
"""

from __future__ import annotations

import math
import random
import threading
import time

from .base import (DC_DIGITS, DC_RATE_HZ, DC_TIME_CONSTANT_S, FLAG_NO_PROBE,
                   FLAG_OK, FLAG_OVERLOAD, MODES, PEAK_DISPLAYS, PEAK_MODES,
                   PROBE_RANGES_mT, RMS_BANDS, UNIT_CODES, pick_peak)
#: RMS noise floor as a fraction of full scale, per resolution. Read off the
#: manual's table: HSE 35 mT range -> 0.0030 / 0.015 / 0.04 G at 5/4/3 digits.
NOISE_FRACTION = {3: 1.1e-4, 4: 4.3e-5, 5: 8.6e-6}


class SimulatedLS455:
    def __init__(self, probe: str = "HSE", field_mT: float = 42.0,
                 ac_rms_mT: float = 0.8, offset_mT: float = 0.05,
                 realtime: bool = False, zero_time_s: float = 1.0,
                 seed: int | None = None, clock=time.monotonic,
                 # -- the meter's own state when the module connects -------------
                 mode: str = "dc", dc_digits: int = 4, rms_band: str = "wide",
                 auto_range: bool = True, range_mT: float | None = None,
                 unit: str = "G", relative: bool = False,
                 rel_setpoint_mT: float = 0.0, peak_mode: str = "periodic",
                 peak_display: str = "positive"):
        if probe not in PROBE_RANGES_mT:
            raise ValueError(f"unknown probe family {probe!r}")
        if (mode not in MODES or int(dc_digits) not in DC_DIGITS or rms_band not in RMS_BANDS
                or unit not in UNIT_CODES or peak_mode not in PEAK_MODES
                or peak_display not in PEAK_DISPLAYS):
            raise ValueError("bad initial sim state")
        self.probe = probe
        self.field_mT = float(field_mT)
        self.ac_rms_mT = float(ac_rms_mT)
        self.offset_mT = float(offset_mT)
        self.realtime = bool(realtime)
        self.zero_time_s = float(zero_time_s)
        self._clock = clock
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

        self._open = False
        self.probe_present = True
        # the 455's factory defaults (manual section 4.14) are DC, autorange on,
        # gauss and 5 digits; the sim defaults to 4 digits so a demo does not wait
        # 7 s per acquisition (the module ADOPTS the meter's settings at start)
        self._mode, self._digits, self._band = mode, int(dc_digits), rms_band
        self._peak = (peak_mode, peak_display)
        self._auto = bool(auto_range)
        self._range = self.ranges_mT()[-1]
        if range_mT is not None:
            self.set_range(range_mT)
        self._unit = unit
        self._rel = (bool(relative), float(rel_setpoint_mT))
        self._zero_until = None
        self._t0 = clock()
        self._filtered = None           # the DC filter's state, mT
        self._t_last = None

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "LSCI,MODEL455,SIM0001,01012026 (simulated)"

    def probe_info(self) -> dict:
        if not self.probe_present:
            return {"family": "", "type_code": -1, "serial": "",
                    "sensitivity_mV_per_kG": float("nan")}
        code = {"HSE": 40, "HST": 41, "UHS": 42}[self.probe]
        return {"family": self.probe, "type_code": code, "serial": f"SIM-{self.probe}",
                "sensitivity_mV_per_kG": {"HSE": 8.0, "HST": 0.8, "UHS": 80.0}[self.probe]}

    def swap_probe(self, probe: str) -> None:
        """Test/demo helper: plug in a different probe family (the range list
        and what RANGE n means change with it)."""
        if probe not in PROBE_RANGES_mT:
            raise ValueError(f"unknown probe family {probe!r}")
        self.probe = probe
        r = self.ranges_mT()
        self._range = next((x for x in r if x >= self._range * 0.999), r[-1])

    def ranges_mT(self) -> list[float]:
        return list(PROBE_RANGES_mT[self.probe])

    # ---- mode ------------------------------------------------------------
    def set_mode(self, mode: str, dc_digits: int, rms_band: str) -> None:
        if mode not in MODES or int(dc_digits) not in DC_DIGITS or rms_band not in RMS_BANDS:
            raise ValueError("bad mode")
        self._mode, self._digits, self._band = mode, int(dc_digits), rms_band

    def get_mode(self) -> tuple[str, int, str]:
        return self._mode, self._digits, self._band

    def get_peak(self) -> tuple[str, str]:
        return self._peak

    # ---- range -----------------------------------------------------------
    def set_auto_range(self, on: bool) -> None:
        if self._auto and not on:
            self._range = self.get_range()     # keep what auto had chosen
        self._auto = bool(on)

    def get_auto_range(self) -> bool:
        return self._auto

    def set_range(self, full_scale_mT: float) -> None:
        r = self.ranges_mT()
        self._range = next((x for x in r if x >= full_scale_mT * 0.999), r[-1])

    def get_range(self) -> float:
        if self._auto:
            b = abs(self._true_value())
            r = self.ranges_mT()
            return next((x for x in r if x >= b * 1.05), r[-1])
        return self._range

    # ---- units / relative ------------------------------------------------
    def set_display_unit(self, unit: str) -> None:
        if unit not in UNIT_CODES:
            raise ValueError(f"unknown unit {unit!r}")
        self._unit = unit

    def get_display_unit(self) -> str:
        return self._unit

    def set_relative(self, on: bool, setpoint_mT: float) -> None:
        self._rel = (bool(on), float(setpoint_mT))

    def get_relative(self) -> tuple[bool, float]:
        return self._rel

    # ---- measurement -----------------------------------------------------
    def _true_value(self) -> float:
        """What an ideal, infinitely fast meter would read right now (mT)."""
        if self._mode == "rms":
            return self.ac_rms_mT
        t = self._clock() - self._t0
        drift = 1.0 + 0.002 * math.sin(2 * math.pi * t / 30.0)
        dc = self.field_mT * drift + self.offset_mT
        if self._mode == "peak":
            amp = self.ac_rms_mT * math.sqrt(2.0)
            return pick_peak(dc + amp, dc - amp, self._peak[1])
        return dc

    def read_field(self) -> tuple[float, str]:
        if not self._open:
            raise RuntimeError("simulated 455 is not open")
        if self.zero_running():
            raise RuntimeError("zero probe in progress")
        if not self.probe_present:
            return float("nan"), FLAG_NO_PROBE
        if self.realtime:
            rate = DC_RATE_HZ[self._digits] if self._mode == "dc" else 30.0
            time.sleep(1.0 / rate)
        x = self._true_value()
        # first-order filter with the manual's time constant
        now = self._clock()
        if self._filtered is None or self._mode != "dc":
            self._filtered = x
        else:
            tau = DC_TIME_CONSTANT_S[self._digits]
            dt = max(0.0, now - (self._t_last if self._t_last is not None else now))
            self._filtered += (x - self._filtered) * (1.0 - math.exp(-dt / tau))
        self._t_last = now
        fs = self.get_range()
        sigma = fs * NOISE_FRACTION[self._digits] + 2e-5 * abs(self._filtered)
        b = self._filtered + self._rng.gauss(0.0, sigma)
        if abs(b) > fs * 1.0 and not self._auto:
            return math.copysign(fs, b), FLAG_OVERLOAD
        return b, FLAG_OK

    # ---- zero ------------------------------------------------------------
    def start_zero(self) -> None:
        # The sim assumes the probe IS in the zero-gauss chamber, so zeroing
        # removes only the probe's own offset, not the field it measures.
        with self._lock:
            self._zero_until = self._clock() + self.zero_time_s
            self.offset_mT = 0.0

    def zero_running(self) -> bool:
        with self._lock:
            if self._zero_until is None:
                return False
            if self._clock() >= self._zero_until:
                self._zero_until = None
                return False
            return True

    def clear_zero(self) -> None:
        self.offset_mT = 0.05
