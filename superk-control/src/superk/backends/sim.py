"""Simulated hardware: a fake SuperK EXTREME + SELECT RF driver.

It implements SupercontinuumBackend from `base`, so the brain cannot tell it
apart from the real laser. It behaves like the registers of the real one:

  * values come back QUANTISED the way the registers store them -- wavelength in
    whole picometres, power level and amplitude in 0.1 % steps -- so a settle
    check with a sensible tolerance is exercised for real;
  * emission takes `warmup_s` to report ON after it is switched on (the seed
    and amplifiers ramp up), and drops at once if the interlock opens;
  * the interlock has the three states of the real one: open -> closed but
    waiting for a reset -> OK. Tests open it with `open_interlock()` and close
    it with `close_interlock()` (a person closing a door, in the lab);
  * temperatures drift a little, the inlet warms up while emitting.

It starts with emission OFF and RF OFF, as a laser that was just powered up.
"""

from __future__ import annotations

import random
import threading
import time

from ..config import N_LINES

# status-bit layout of the EXTREME (# VERIFY, see backends/nktp.py)
BIT_EMISSION = 0x0001
BIT_INTERLOCK_OFF = 0x0002
BIT_INTERLOCK_LOOP_OPEN = 0x0008
BIT_ERROR = 0x8000


class SimulatedSuperK:
    """Pretends to be a SuperK EXTREME with a SELECT RF driver."""

    def __init__(self, crystal_ranges: dict[int, tuple[float, float]] | None = None,
                 warmup_s: float = 1.5, report_range: bool = False):
        # NKT crystal number -> (min_nm, max_nm). With report_range=False
        # the driver answers None, like a driver with no crystal info, and the
        # brain falls back to the config (the default: the config is the truth
        # in simulation).
        # NKT numbering: 1, 2 = SELECT (VIS-nIR, nIR2), 4 = slot 2 of the
        # SELECT2 housing (IR; its slot 1 is empty, hence "-/IR").
        self._ranges = dict(crystal_ranges or {1: (500.0, 900.0), 2: (800.0, 1400.0),
                                               4: (1100.0, 2000.0)})
        self._report_range = report_range
        self.warmup_s = float(warmup_s)
        self._lock = threading.Lock()
        self._open = False
        # EXTREME
        self._emission_cmd = False
        self._emission_since = 0.0
        self._power = 0.0                  # per-mille in the register, % here
        self._interlock_closed = True
        self._interlock_code = 2           # 2 = OK
        self._watchdog = 0
        self._inlet = 23.5
        # RF driver
        self._rf = False
        self._crystal = next(iter(self._ranges))
        self._wl = [0.0] * N_LINES
        self._amp = [0.0] * N_LINES
        self._xtal_temp = 26.0

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        with self._lock:
            self._emission_cmd = False
            self._rf = False
        self._open = False

    def identify(self) -> str:
        return ("SuperK EXTREME (sim) + SELECT RF driver (sim)") if self._open else ""

    # ---- test hooks: a person at the interlock -----------------------------
    def open_interlock(self) -> None:
        with self._lock:
            self._interlock_closed = False
            self._interlock_code = 0
            self._emission_cmd = False     # the hardware cuts emission itself

    def close_interlock(self) -> None:
        with self._lock:
            self._interlock_closed = True
            if self._interlock_code == 0:
                self._interlock_code = 1   # closed, but needs a reset

    # ---- EXTREME -----------------------------------------------------------
    def set_emission(self, on: bool) -> None:
        with self._lock:
            if on and self._interlock_code != 2:
                return                     # the real laser ignores it too
            if on and not self._emission_cmd:
                self._emission_since = time.monotonic()
            self._emission_cmd = bool(on)

    def read_emission(self) -> bool:
        with self._lock:
            return (self._emission_cmd
                    and time.monotonic() - self._emission_since >= self.warmup_s)

    def read_interlock(self) -> int:
        with self._lock:
            return self._interlock_code

    def reset_interlock(self) -> None:
        with self._lock:
            if self._interlock_closed:
                self._interlock_code = 2

    def read_status_bits(self) -> int:
        bits = 0
        if self.read_emission():
            bits |= BIT_EMISSION
        with self._lock:
            if self._interlock_code != 2:
                bits |= BIT_INTERLOCK_OFF
            if not self._interlock_closed:
                bits |= BIT_INTERLOCK_LOOP_OPEN
        return bits

    def set_power(self, pct: float) -> None:
        with self._lock:
            self._power = round(float(pct) * 10) / 10      # 0.1 % register steps

    def read_power(self) -> float:
        with self._lock:
            return self._power

    def read_inlet_temp(self) -> float:
        with self._lock:
            target = 23.5 + (4.0 * self._power / 100 if self._emission_cmd else 0.0)
            self._inlet += 0.05 * (target - self._inlet) + random.gauss(0, 0.02)
            return round(self._inlet, 1)

    def set_watchdog(self, seconds: int) -> None:
        self._watchdog = int(seconds)

    # ---- RF driver ---------------------------------------------------------
    def set_rf(self, on: bool) -> None:
        with self._lock:
            self._rf = bool(on)

    def read_rf(self) -> bool:
        with self._lock:
            return self._rf

    def select_crystal(self, crystal: int) -> None:
        # Behave like the hardware's two rules: the RF switch must not move
        # under RF power, and a crystal that is not there cannot be reached.
        with self._lock:
            crystal = int(crystal)
            if crystal == self._crystal:
                return
            if self._rf:
                raise RuntimeError("RF power must be off before the crystal is changed")
            if crystal not in self._ranges:
                raise RuntimeError(f"crystal {crystal} is not connected to the RF driver")
            self._crystal = crystal

    def read_crystal(self) -> int:
        with self._lock:
            return self._crystal

    def read_crystal_range(self):
        if not self._report_range:
            return None
        with self._lock:
            return self._ranges.get(self._crystal)

    def read_crystal_temp(self) -> float:
        with self._lock:
            load = sum(self._amp) / (100.0 * N_LINES) if self._rf else 0.0
            target = 26.0 + 6.0 * load
            self._xtal_temp += 0.05 * (target - self._xtal_temp) + random.gauss(0, 0.02)
            return round(self._xtal_temp, 1)

    def set_wavelength(self, ch: int, nm: float) -> None:
        with self._lock:
            self._wl[ch] = round(float(nm) * 1000) / 1000   # whole picometres

    def read_wavelength(self, ch: int) -> float:
        with self._lock:
            return self._wl[ch]

    def set_amplitude(self, ch: int, pct: float) -> None:
        with self._lock:
            self._amp[ch] = round(float(pct) * 10) / 10      # 0.1 % steps

    def read_amplitude(self, ch: int) -> float:
        with self._lock:
            return self._amp[ch]
