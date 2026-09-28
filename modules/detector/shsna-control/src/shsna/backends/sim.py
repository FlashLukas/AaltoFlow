"""The simulated scalar network analyser: a TG sweep through the pretend chain
of `config.Sim`, standalone (no signalhound service needed).

The physics is in `shsna.physics`; this file adds what makes it an INSTRUMENT:
an acquisition takes time (the lab TG44A's measured 0.2 s + 1.3 ms per point
per sweep), the chain is LATCHED when it starts (so inserting the DUT halfway
through a sweep cannot hand back a trace that is half thru and half filter),
the point count is clamped to 1001 like the real API does, and there is noise.
Traces are in dB relative to the TG output, as the real TG44A reports them.

THE FILM'S FIELD (sim.fmr_on, 2026-09-28). The optional magnetic film needs
the field it sits in; `field.py` supplies it (a magnet service's status
stream, or a manual value). The source is built only while the film is on,
rebuilt when its config changes, and READ -- a cached copy, never a wait --
when a sweep starts: the field is latched with the chain, so a magnet that
moves during a sweep does not smear one trace over two fields.
"""

from __future__ import annotations

import copy
import math
import threading
import time

import numpy as np

from .. import physics
from ..config import Config
from ..field import make_field_source
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
        self._field = None              # the film's field source (field.py), while fmr_on
        self._field_key = None          # the config it was built from
        # sync_field runs from the sweep thread AND from a setter (so status is
        # right at once): one at a time, or both could build a subscriber
        self._field_lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self._open = True
        self.sync_field()

    def close(self) -> None:
        self._open = False
        self._pending = None
        with self._field_lock:
            self._close_field()

    def idn(self) -> str:
        return "AaltoFlow simulated scalar network analyser (TG -> pad -> DUT -> SA; not a real instrument)"

    # ---- the film's field ------------------------------------------------------
    def _field_config_key(self):
        f = self.cfg.field
        return (f.source, f.mag2d_host, int(f.mag2d_pub_port), f.mag2dcal_host,
                int(f.mag2dcal_pub_port), f.clMag_host, int(f.clMag_pub_port),
                f.ppms_host, int(f.ppms_pub_port), float(f.stale_s))

    def _close_field(self) -> None:
        if self._field is not None:
            try:
                self._field.close()
            except Exception:
                pass
        self._field, self._field_key = None, None

    def sync_field(self) -> None:
        """Build / rebuild / drop the field source so it matches the config.
        Called from the brain's sweep thread (through health()) and when a
        sweep starts -- cheap when nothing changed. A remote source costs a
        subscriber thread, so it exists only while the film is on."""
        with self._field_lock:
            want = bool(self.cfg.sim.fmr_on) and self._open
            if not want:
                if self._field is not None:
                    self._close_field()
                return
            key = self._field_config_key()
            if self._field is not None and key == self._field_key:
                return
            self._close_field()
            try:
                field = make_field_source(self.cfg.field)
            except ValueError:
                field = None             # an unknown source name: no field, status says so
            self._field, self._field_key = field, key

    def sim_field(self) -> dict:
        """What the film sits in now, for status: field, angle, the source (and
        its health), and the Kittel frequency. NaN / "" while the film is off.
        Reads a cached value only (never blocks)."""
        nan = float("nan")
        if not self.cfg.sim.fmr_on:
            return {"field_mT": nan, "angle_deg": nan, "fres_Hz": nan, "fwhm_Hz": nan,
                    "source": "", "ok": False}
        fs = self._field                  # one read: another thread may rebuild it
        if fs is None:
            return {"field_mT": nan, "angle_deg": nan, "fres_Hz": nan, "fwhm_Hz": nan,
                    "source": f"no field source ({self.cfg.field.source!r})", "ok": False}
        r = fs.read()
        fr, fwhm = physics.fmr_resonance(r.field_mT, r.angle_deg, self.cfg.sim)
        return {"field_mT": float(r.field_mT), "angle_deg": float(r.angle_deg),
                "fres_Hz": fr, "fwhm_Hz": fwhm, "source": r.source, "ok": bool(r.ok)}

    def health(self) -> str:
        self.sync_field()
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
        self.sync_field()
        fld = self.sim_field()
        self._pending = {
            "grid": self.predicted_grid(start_Hz, stop_Hz, points),
            "rbw": float(rbw_Hz), "averages": max(1, int(averages)),
            "sim": copy.copy(self.cfg.sim),          # latched: see the module docstring
            "field": (fld["field_mT"], fld["angle_deg"]),   # latched with it
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
        b, a = p["field"]
        sweeps = [physics.tg_sweep_dB(f, p["rbw"], p["sim"], self._rng,
                                      b, a if math.isfinite(a) else 0.0)
                  for _ in range(p["averages"])]
        db = physics.power_mean_db(sweeps)
        return {"start_Hz": start, "bin_Hz": bin_Hz, "points": n,
                "rbw_Hz": p["rbw"], "averages": p["averages"], "db": db,
                # a passive chain cannot put out more than the TG does; far
                # above the TG's own output something is wrong (an amplifier?)
                "overload": bool((db > 15.0).any())}

    def abort(self) -> None:
        self._pending = None
