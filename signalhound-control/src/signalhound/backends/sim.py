"""The simulated Signal Hound: an SA44B (or SA124B) with a USB-TG44A, looking
at the scene in `cfg.scene` (a generator with harmonics, or a filter behind the
tracking generator).

The physics is in `signalhound.physics`; this file adds what makes it an
INSTRUMENT -- it is configured before it sweeps, it chooses its own bin grid,
sweeps take time, and the scene is latched when a sweep starts (so changing
the filter halfway through a 10-second sweep cannot hand back a trace that is
half one filter and half another).
"""

from __future__ import annotations

import copy

import numpy as np

from .. import physics
from ..config import Config
from ..instruments import Grid, SweepSettings, estimate_sweep_time_s, sim_grid, MODELS


class SimulatedAnalyzer:
    simulated = True

    def __init__(self, cfg: Config, seed: int | None = None, time_scale: float = 1.0,
                 tg_output_on: bool = False):
        """`time_scale` multiplies every sweep time: 1.0 behaves like the real
        analyser (the GUI, the service), 0.0 makes tests instant.

        `tg_output_on` is the state the TG was LEFT in by whatever used the
        analyser before us (the real TG44A may keep emitting CW, VERIFY 5b in
        the module notes). open() does not change it -- the start-up rule --
        so a test can start from a non-default state and see it untouched."""
        self.cfg = cfg
        self.time_scale = float(time_scale)
        self._rng = np.random.default_rng(seed)
        self._open = False
        self._settings: SweepSettings | None = None
        self._grid: Grid | None = None
        self._pending = None
        self.tg_output_on = bool(tg_output_on)   # for the safety tests: is the TG emitting?
        # Every configure() is a WRITE to the analyser (settings + initiate);
        # tests count them to prove that start-up writes nothing.
        self.configure_calls = 0

    # ---- lifecycle ---------------------------------------------------------
    def _model(self) -> str:
        m = self.cfg.hardware.model
        return m if m in MODELS else "SA44B"          # "auto" -> the SA44B

    def open(self) -> None:
        # Like the real one: opening reads (model, TG present) and writes
        # nothing, so whatever the TG was doing it keeps doing.
        self._open = True

    def close(self) -> None:
        self._open = False
        self._pending = None
        self.tg_output_on = False

    def idn(self) -> str:
        return f"AaltoFlow simulated Signal Hound {self._model()} (not a real instrument)"

    def device_model(self) -> str:
        return self._model()

    def tg_attached(self) -> bool:
        return bool(self.cfg.scene.tg_attached)

    # ---- sweeping ----------------------------------------------------------
    def configure(self, settings: SweepSettings) -> Grid:
        if not self._open:
            raise RuntimeError("simulated analyser is not open")
        if settings.tg_on and not self.tg_attached():
            raise RuntimeError("no tracking generator attached")
        self.configure_calls += 1
        self._settings = settings
        self._grid = sim_grid(settings, self.cfg.limits.max_bins)
        self._pending = None
        self.tg_output_on = settings.tg_on
        return self._grid

    def sweep_time_s(self, settings: SweepSettings, points: int) -> float:
        return estimate_sweep_time_s(settings, points) * self.time_scale

    def start_sweep(self) -> None:
        if self._settings is None:
            raise RuntimeError("start_sweep before configure")
        self._pending = {"settings": self._settings, "grid": self._grid,
                         "scene": copy.copy(self.cfg.scene), "model": self._model()}

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        p, self._pending = self._pending, None
        if p is None:
            raise RuntimeError("finish_sweep without start_sweep")
        s, g = p["settings"], p["grid"]
        if s.tg_on:
            db, over = physics.tracking_dBm(g.freqs(), s, p["scene"], self._rng)
        else:
            db, over = physics.spectrum_dBm(g.freqs(), g.bin_Hz, s, p["scene"], p["model"],
                                            self._rng)
        return db, {"overload": over}

    def abort_sweep(self) -> None:
        self._pending = None
