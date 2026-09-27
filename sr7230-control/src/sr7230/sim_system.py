"""Build a fully-simulated lock-in in one call. Qt-free on purpose."""

from __future__ import annotations

import time

from .backends.sim import Simulated7230
from .config import Config
from .lockin import LockIn


def build_sim_system(cfg: Config | None = None, clock=time.monotonic,
                     seed: int | None = None) -> tuple[LockIn, Simulated7230]:
    """Return (lockin, sim_backend) wired together but NOT yet started."""
    cfg = cfg or Config()
    backend = Simulated7230(clock=clock, seed=seed)
    # Put the simulated external source at the frequency the oscillator STARTS
    # on, so a fresh simulator shows a signal on the internal reference. Tuning
    # away still makes it vanish, as on the real instrument.
    backend.signal_Hz = cfg.reference.frequency_Hz
    return LockIn(backend, cfg, clock=clock), backend
