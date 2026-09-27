"""A simulated PM400 console with swappable heads, so everything runs with
nothing plugged in.

It reads its bench from a `config.Sim` object LIVE, so changing `sim.head` in
Settings is exactly like unplugging one head and plugging in another: the
console notices, loads the new head's defaults, and the module's limits, units
and `describe` follow.

Enough physics that the controls visibly DO something:

  photodiode (S121C-like, Si, 400-1100 nm)
      The console turns photocurrent into watts with the responsivity at the
      wavelength it is TOLD, so 800 nm light read at 1064 nm reads high -- as
      on the bench. Fast: every reading is new light.
  thermal (S302C-like, 190 nm - 25 um)
      Nearly flat absorption, so the wavelength setting matters by a few %
      only. But the absorber is SLOW: the reading follows the light with a
      ~1 s time constant, which is why `acquisition.settle_s` exists. It also
      has a thermal zero offset of tens of uW until you zero it.
  pyro (ES111C-like, energy per pulse)
      A pulsed laser at `rep_rate_Hz`; each reading is ONE pulse (blocks until
      the next pulse in real time), with 2 % pulse-to-pulse scatter. No auto
      range and no zero, as on the real heads.
  none
      Nothing plugged in: every measurement raises.

Readings sleep only when `realtime=True` (the GUI and the service); tests pass
False and run fast.
"""

from __future__ import annotations

import math
import random
import threading
import time

from .base import (FLAG_NAN, FLAG_OK, FLAG_OVERRANGE, HEAD_NONE, HEAD_PHOTODIODE,
                   HEAD_PYRO, HEAD_THERMAL, empty_sensor_info)

# Approximate Si photodiode responsivity (A/W), roughly the shape of Thorlabs'
# S12x curves: rising with wavelength, peaking near 960 nm, falling to 1100 nm.
_RESPONSIVITY = [(400, 0.12), (500, 0.24), (600, 0.33), (700, 0.42),
                 (800, 0.50), (900, 0.58), (960, 0.61), (1000, 0.58),
                 (1064, 0.40), (1100, 0.25)]

# The simulated heads. `ranges` are the console's range steps for that head, in
# W (power heads) or J (the pyro head). `tau_s` is the thermal response time.
HEADS = {
    HEAD_PHOTODIODE: {
        "name": "S121C (sim)", "wl": (400.0, 1100.0), "wl_default": 635.0,
        "ranges": [1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 0.5],
        "energy": False, "zero": True, "noise_rel": 0.002, "floor": 2e-9,
        "offset": 2e-9, "tau_s": 0.0},
    HEAD_THERMAL: {
        "name": "S302C (sim)", "wl": (190.0, 25000.0), "wl_default": 1064.0,
        "ranges": [2e-3, 2e-2, 0.2, 2.0],
        "energy": False, "zero": True, "noise_rel": 0.003, "floor": 3e-6,
        "offset": 4e-5, "tau_s": 1.0},
    HEAD_PYRO: {
        "name": "ES111C (sim)", "wl": (185.0, 25000.0), "wl_default": 1064.0,
        "ranges": [1.5e-4, 1.5e-3, 1.5e-2, 0.15],
        "energy": True, "zero": False, "noise_rel": 0.02, "floor": 2e-7,
        "offset": 0.0, "tau_s": 0.0},
}

AVG_LIMITS_S = (0.001, 10.0)       # what the simulated console accepts


def responsivity(nm: float) -> float:
    """Si photodiode, piecewise-linear in the table above, clamped at the ends."""
    pts = _RESPONSIVITY
    if nm <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if nm <= x1:
            return y0 + (y1 - y0) * (nm - x0) / (x1 - x0)
    return pts[-1][1]


def absorption(nm: float) -> float:
    """A black broadband absorber (thermal / pyro): ~0.95, a gentle few-% slope,
    so a wrong wavelength setting gives a SMALL error, not a big one."""
    return 0.95 - 0.03 * math.tanh((nm - 2000.0) / 3000.0)


def _spectral(kind: str, nm: float) -> float:
    return responsivity(nm) if kind == HEAD_PHOTODIODE else absorption(nm)


class SimulatedPM400:
    def __init__(self, sim_cfg, realtime: bool = False, zero_time_s: float = 1.0,
                 seed: int | None = None, clock=time.monotonic):
        self.sim = sim_cfg                  # a config.Sim, read live
        self.realtime = bool(realtime)
        self.zero_time_s = float(zero_time_s)
        self._clock = clock
        self._rng = random.Random(seed)
        self._lock = threading.RLock()
        self._open = False
        self._t0 = clock()
        self._head = None
        self._load_head(self.sim.head)

    # ---- the head the "user" plugged in -----------------------------------
    def _spec(self) -> dict | None:
        return HEADS.get(self._head)

    def _load_head(self, kind: str) -> None:
        """What a console does when a head is plugged in: load its defaults."""
        kind = kind if kind in HEADS or kind == HEAD_NONE else HEAD_NONE
        self._head = kind
        spec = HEADS.get(kind)
        self._wl = spec["wl_default"] if spec else 0.0
        self._auto = spec is not None and not spec["energy"]
        self._range = spec["ranges"][-1] if spec else float("nan")
        self._avg = 0.1
        self._offset = spec["offset"] if spec else 0.0     # un-zeroed offset
        self._stored = 0.0                  # what the last zero subtracts (W)
        self._zero_until = None
        self._th_val = 0.0                  # the thermal absorber starts cold
        self._th_t = self._clock()

    def _sync_head(self) -> None:
        with self._lock:
            if str(self.sim.head) != self._head:
                self._load_head(str(self.sim.head))

    def _need_head(self) -> dict:
        self._sync_head()
        spec = self._spec()
        if spec is None:
            raise RuntimeError("no sensor head connected")
        return spec

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "Thorlabs PM400 (simulated) S/N SIM0400 fw 1.0.0"

    def sensor_info(self) -> dict:
        self._sync_head()
        spec = self._spec()
        if spec is None:
            return empty_sensor_info()
        return {"kind": self._head, "name": spec["name"], "serial": "",
                "energy": spec["energy"], "wavelength_settable": True,
                "zero_supported": spec["zero"]}

    # ---- wavelength ------------------------------------------------------
    def set_wavelength(self, nm: float) -> None:
        self._need_head()
        self._wl = float(nm)

    def get_wavelength(self) -> float:
        self._sync_head()
        return self._wl if self._spec() else float("nan")

    def wavelength_range(self) -> tuple[float, float]:
        return self._need_head()["wl"]

    # ---- power range -----------------------------------------------------
    def set_auto_range(self, on: bool) -> None:
        spec = self._need_head()
        if spec["energy"]:
            raise RuntimeError("energy heads have no auto range")
        if self._auto and not on:
            self._range = self.get_range()     # leaving auto keeps its choice
        self._auto = bool(on)

    def get_auto_range(self) -> bool:
        self._sync_head()
        return self._auto

    def set_range(self, watts: float) -> None:
        spec = self._need_head()
        if spec["energy"]:
            raise RuntimeError("this is an energy head; set the energy range")
        self._range = self._snap(spec, watts)

    def get_range(self) -> float:
        spec = self._need_head()
        if spec["energy"]:
            return float("nan")
        if self._auto:
            p = abs(self._true_power(spec))
            return next((r for r in spec["ranges"] if r >= p), spec["ranges"][-1])
        return self._range

    def range_limits(self) -> tuple[float, float]:
        spec = self._need_head()
        if spec["energy"]:
            return (float("nan"), float("nan"))
        return (spec["ranges"][0], spec["ranges"][-1])

    # ---- energy range ------------------------------------------------------
    def set_energy_range(self, joules: float) -> None:
        spec = self._need_head()
        if not spec["energy"]:
            raise RuntimeError("this is a power head; set the power range")
        self._range = self._snap(spec, joules)

    def get_energy_range(self) -> float:
        spec = self._need_head()
        return self._range if spec["energy"] else float("nan")

    def energy_range_limits(self) -> tuple[float, float]:
        spec = self._need_head()
        if not spec["energy"]:
            return (float("nan"), float("nan"))
        return (spec["ranges"][0], spec["ranges"][-1])

    @staticmethod
    def _snap(spec: dict, value: float) -> float:
        """A real console snaps to its next range UP; so does this one."""
        return next((r for r in spec["ranges"] if r >= value * 0.999), spec["ranges"][-1])

    # ---- averaging -------------------------------------------------------
    def set_avg_time(self, seconds: float) -> None:
        self._need_head()
        self._avg = min(max(float(seconds), AVG_LIMITS_S[0]), AVG_LIMITS_S[1])

    def get_avg_time(self) -> float:
        return self._avg

    def avg_time_limits(self) -> tuple[float, float]:
        return AVG_LIMITS_S

    # ---- measurement -----------------------------------------------------
    def _true_power(self, spec: dict) -> float:
        """The noiseless value the console would display right now (W)."""
        return self._light(spec) + self._offset

    def _light(self, spec: dict) -> float:
        """What the incident light alone makes the console display (W), without
        the head's dark offset. The thermal head's lag lives here."""
        t = self._clock() - self._t0
        drift = 1.0 + 0.01 * math.sin(2 * math.pi * t / 20.0)       # slow 1 % laser drift
        kind = self._head
        seen = float(self.sim.incident_W) * drift * _spectral(kind, float(self.sim.laser_nm))
        target = seen / _spectral(kind, self._wl)
        if spec["tau_s"] > 0:
            # first-order lag of the thermal absorber
            now = self._clock()
            dt = max(0.0, now - self._th_t)
            self._th_val += (target - self._th_val) * (1.0 - math.exp(-dt / spec["tau_s"]))
            self._th_t = now
            target = self._th_val
        return target

    def measure_power(self) -> tuple[float, str]:
        self._check_open()
        spec = self._need_head()
        if spec["energy"]:
            raise RuntimeError("this is an energy head; measure energy")
        if self.zero_running():
            raise RuntimeError("zero adjustment in progress")
        if self.realtime:
            time.sleep(self._avg)
        with self._lock:
            p = self._true_power(spec)
        # longer averaging -> less noise, as 1/sqrt(averaging time)
        sigma = (spec["noise_rel"] * abs(p) + spec["floor"]) * math.sqrt(0.1 / self._avg)
        p += self._rng.gauss(0.0, sigma)
        if not self._auto and p > self._range * 1.1:
            return self._range * 1.1, FLAG_OVERRANGE
        return p, FLAG_OK

    def measure_energy(self) -> tuple[float, str]:
        self._check_open()
        spec = self._need_head()
        if not spec["energy"]:
            raise RuntimeError("this is a power head; measure power")
        rate = float(self.sim.rep_rate_Hz)
        if rate <= 0:
            if self.realtime:
                time.sleep(0.5)
            return float("nan"), FLAG_NAN                    # no pulses arrive
        if self.realtime:
            # wait for the NEXT pulse: a reading is always a new pulse
            period = 1.0 / rate
            t = self._clock() - self._t0
            time.sleep(period - (t % period))
        seen = float(self.sim.pulse_energy_J) * absorption(float(self.sim.laser_nm))
        e = seen / absorption(self._wl)
        e += self._rng.gauss(0.0, spec["noise_rel"] * e + spec["floor"])
        if e > self._range * 1.1:
            return self._range * 1.1, FLAG_OVERRANGE
        return e, FLAG_OK

    def measure_frequency(self) -> float:
        spec = self._need_head()
        return float(self.sim.rep_rate_Hz) if spec["energy"] else 0.0

    def _check_open(self) -> None:
        if not self._open:
            raise RuntimeError("simulated PM400 is not open")

    # ---- zero ------------------------------------------------------------
    def start_zero(self) -> None:
        spec = self._need_head()
        if not spec["zero"]:
            raise RuntimeError("energy heads do not support zero adjustment")
        with self._lock:
            # The new zero is taken when the adjustment FINISHES (zero_running),
            # from whatever reaches the head then -- light included, exactly as
            # on the real console. That is why the head must be covered.
            self._zero_until = self._clock() + self.zero_time_s

    def cancel_zero(self) -> None:
        with self._lock:
            self._zero_until = None

    def zero_running(self) -> bool:
        with self._lock:
            if self._zero_until is None:
                return False
            if self._clock() >= self._zero_until:
                self._zero_until = None
                self._finish_zero()
                return False
            return True

    def _finish_zero(self) -> None:
        """Store what the head sees now (dark offset + any light) as the zero."""
        spec = self._spec()
        if spec is None:
            return
        base = spec["offset"]
        self._stored = base + self._light(spec)
        self._offset = base - self._stored

    def dark_offset(self) -> float:
        """The stored offset in the head's own unit: A for a photodiode, V for a
        thermopile (0 until the first zero adjustment)."""
        spec = self._need_head()
        if not spec["zero"]:
            return float("nan")
        stored = self._stored
        if self._head == HEAD_PHOTODIODE:
            return stored * responsivity(self._wl)           # W -> A
        return stored * 0.1                                   # W -> V at ~0.1 V/W
