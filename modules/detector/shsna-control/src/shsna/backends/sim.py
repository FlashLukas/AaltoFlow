"""The simulated scalar network analyser: a TG sweep through the pretend chain
of `config.Sim`, standalone (no signalhound service needed).

The physics is in `shsna.physics`; this file adds what makes it an INSTRUMENT:
an acquisition takes time (the lab TG44A's measured 0.2 s + 1.3 ms per point
per sweep), the chain is LATCHED when it starts (so inserting the DUT halfway
through a sweep cannot hand back a trace that is half thru and half filter),
the point count is clamped to 1001 like the real API does, and there is noise.
Traces are in dB relative to the TG output, as the real TG44A reports them.
"""

from __future__ import annotations

import copy
import time

import numpy as np

from .. import physics
from ..config import Config
from .base import SweepFailed

#: one TG sweep on the lab's SA + TG44A (measured 2026-09-28): 0.2 s + 1.3 ms
#: per point (101 points 0.36 s, 1001 points 1.50 s)
_S_FIXED, _S_PER_POINT = 0.2, 1.3e-3
#: the SA API clamps a TG sweep to this many points, silently
MAX_POINTS = 1001


class SimulatedSna:
    simulated = True

    def __init__(self, cfg: Config, seed: int | None = None, time_scale: float = 1.0,
                 clock=time.monotonic):
        """`time_scale` multiplies every sweep time: 1.0 behaves like the real
        kit (the GUI, the service), 0.0 makes tests instant."""
        self.cfg = cfg
        self.time_scale = float(time_scale)
        self._clock = clock
        self._rng = np.random.default_rng(seed)
        self._open = False
        self._pending: dict | None = None
        #: a test can make the NEXT start_sweep fail with this reason, to prove
        #: that a failed acquisition is latched as failed
        self.fail_next = ""

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False
        self._pending = None

    def idn(self) -> str:
        return "AaltoFlow simulated scalar network analyser (TG -> pad -> DUT -> SA; not a real instrument)"

    def health(self) -> str:
        if not self._open:
            return "not open"
        if not self.cfg.sim.tg_attached:
            return "no tracking generator attached (simulated)"
        return ""

    def owner_status(self) -> dict:
        return {"address": "simulated", "reachable": self._open,
                "tg_attached": bool(self.cfg.sim.tg_attached),
                "tg_mode": "sweep" if self._pending else "parked", "hw_error": ""}

    # ---- sweeping ------------------------------------------------------------
    def estimate_time_s(self, points, averages) -> float:
        n = min(MAX_POINTS, max(2, int(points)))
        return (_S_FIXED + n * _S_PER_POINT) * max(1, int(averages)) * self.time_scale

    def predicted_grid(self, start_Hz, stop_Hz, points):
        n = min(MAX_POINTS, max(2, int(points)))
        return float(start_Hz), (float(stop_Hz) - float(start_Hz)) / (n - 1), n

    def start_sweep(self, start_Hz, stop_Hz, points, rbw_Hz, averages) -> None:
        if not self._open:
            raise ConnectionError("simulated analyser is not open")
        if self.fail_next:
            why, self.fail_next = self.fail_next, ""
            raise SweepFailed(why)
        if not self.cfg.sim.tg_attached:
            raise SweepFailed("no tracking generator attached")
        if self._pending is not None:
            raise SweepFailed("a TG sweep is already running")
        self._pending = {
            "grid": self.predicted_grid(start_Hz, stop_Hz, points),
            "rbw": float(rbw_Hz), "averages": max(1, int(averages)),
            "sim": copy.copy(self.cfg.sim),          # latched: see the module docstring
            "t_done": self._clock() + self.estimate_time_s(points, averages),
        }

    def poll(self) -> bool:
        if self._pending is None:
            raise SweepFailed("no TG sweep running")
        return self._clock() >= self._pending["t_done"]

    def fetch(self) -> dict:
        p, self._pending = self._pending, None
        if p is None:
            raise SweepFailed("fetch without a finished TG sweep")
        start, bin_Hz, n = p["grid"]
        f = start + bin_Hz * np.arange(n)
        sweeps = [physics.tg_sweep_dB(f, p["rbw"], p["sim"], self._rng)
                  for _ in range(p["averages"])]
        db = physics.power_mean_db(sweeps)
        return {"start_Hz": start, "bin_Hz": bin_Hz, "points": n,
                "rbw_Hz": p["rbw"], "averages": p["averages"], "db": db,
                # a passive chain cannot put out more than the TG does; far
                # above the TG's own output something is wrong (an amplifier?)
                "overload": bool((db > 15.0).any())}

    def abort(self) -> None:
        self._pending = None
