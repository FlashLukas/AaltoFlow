"""The simulated CCS200: a lamp and a few emission lines on its fibre, read by
a CCD with offset, dark current and noise. The physics is in `ccs200.model`;
this file adds what makes it an INSTRUMENT -- a scan takes time, and the
conditions are latched when it starts.

Latching at `start_scan` matters: the sim settings are COPIED there, so
switching the light off halfway through a 5-second exposure cannot hand back
a scan that is half one world and half another. (A real CCD would integrate the
mixture; the brain throws such a scan away anyway when a setting changes.)
"""

from __future__ import annotations

import copy
import time

import numpy as np

from .. import model
from ..config import Config


class SimulatedSpectrometer:
    simulated = True

    def __init__(self, cfg: Config, seed: int | None = None, time_scale: float = 1.0,
                 clock=time.monotonic):
        """`time_scale` multiplies every scan time: 1.0 behaves like the real
        instrument (the GUI, the service), 0.0 makes tests instant."""
        self.cfg = cfg
        self.time_scale = float(time_scale)
        self._clock = clock
        self._rng = np.random.default_rng(seed)
        self._wl = model.pixel_wavelengths()
        self._pattern = model.dark_pattern(self._wl.size)
        self._open = False
        self._pending = None

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False
        self._pending = None

    def idn(self) -> str:
        return "AaltoFlow simulated CCS200 (lamp + Hg/Ar lines; not a real instrument)"

    def wavelengths(self) -> np.ndarray:
        return self._wl.copy()

    # ---- scanning ------------------------------------------------------------
    def start_scan(self, integration_s: float) -> None:
        if not self._open:
            raise RuntimeError("simulated spectrometer is not open")
        t = float(integration_s)
        self._pending = {"t": t, "sim": copy.copy(self.cfg.sim),
                         "t0": self._clock(),
                         "dt": model.scan_duration_s(t) * self.time_scale}

    def scan_ready(self) -> bool:
        p = self._pending
        return p is not None and self._clock() - p["t0"] >= p["dt"]

    def read_scan(self) -> np.ndarray:
        p, self._pending = self._pending, None
        if p is None:
            raise RuntimeError("read_scan without start_scan")
        return model.scan(self._wl, p["sim"], p["t"], self._pattern, self._rng)

    def abort_scan(self) -> None:
        self._pending = None

    def busy(self) -> bool:
        """Never: an abandoned simulated scan simply vanishes (see base.busy)."""
        return False
