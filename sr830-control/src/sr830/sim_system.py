"""Build a fully-simulated SR830 in one call. Qt-free on purpose."""

from __future__ import annotations

import time

from .backends.sim import SimulatedSR830
from .config import Config
from .lockin import DspLockIn


def build_sim_system(cfg: Config | None = None, clock=time.monotonic,
                     seed: int | None = None) -> tuple[DspLockIn, SimulatedSR830]:
    """Return (lockin, sim_backend) wired together but NOT yet started."""
    cfg = cfg or Config()
    backend = SimulatedSR830(clock=clock, seed=seed)
    # Put the simulated experiment at the frequency the lock-in STARTS on, so a
    # fresh simulator shows a signal. Tuning away still makes it vanish, as on
    # the real instrument.
    backend.ext_ref_Hz = cfg.reference.frequency_Hz
    return DspLockIn(backend, cfg, clock=clock), backend
