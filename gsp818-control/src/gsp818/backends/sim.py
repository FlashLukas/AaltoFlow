"""The simulated GSP-818: a noise floor that follows RBW, VBW, attenuation and
the preamp, a few carriers on the input, and -- tracking generator on -- a
device under test (the physics is in `gsp818.model`).

This file adds what makes it an INSTRUMENT: sweeps take time, and the bench is
latched when a sweep starts, so changing the DUT halfway through a 10-second
sweep cannot hand back a trace that is half one filter and half another.
"""

from __future__ import annotations

import copy

import numpy as np

from .. import model
from ..config import Config
from ..model import SweepSettings


class SimulatedAnalyzer:
    simulated = True

    def __init__(self, cfg: Config, seed: int | None = None, time_scale: float = 1.0):
        """`time_scale` multiplies every sweep time: 1.0 behaves like the real
        analyser (the GUI, the service), 0.0 makes tests instant."""
        self.cfg = cfg
        self.time_scale = float(time_scale)
        self._rng = np.random.default_rng(seed)
        self._open = False
        self._pending = None
        self.tg_output = False          # what the pretend GEN OUTPUT is doing (tests check it)

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self._open = True
        self.tg_output = False          # the safe state, as the real one after open()

    def close(self) -> None:
        self.tg_output = False
        self._open = False
        self._pending = None

    def idn(self) -> str:
        return "AaltoFlow,simulated GSP-818 spectrum analyser,0,sim (not a real instrument)"

    # ---- sweeping ------------------------------------------------------------
    def configure(self, s: SweepSettings) -> dict:
        if not self._open:
            raise RuntimeError("simulated analyser is not open")
        self.tg_output = bool(s.tg_on)
        return {"rbw_Hz": s.rbw_Hz, "vbw_Hz": s.vbw_Hz, "atten_dB": s.atten_dB,
                "sweep_time_s": s.sweep_time_s}

    def start_sweep(self, s: SweepSettings) -> float:
        if not self._open:
            raise RuntimeError("simulated analyser is not open")
        self._pending = (s, copy.copy(self.cfg.bench))
        return s.sweep_time_s * self.time_scale

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        p, self._pending = self._pending, None
        if p is None:
            raise RuntimeError("finish_sweep without start_sweep")
        s, bench = p
        return model.simulate(s, bench, self._rng)

    def abort_sweep(self) -> None:
        self._pending = None
