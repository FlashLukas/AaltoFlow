"""Build a fully-simulated lock-in in one call. Qt-free on purpose."""

from __future__ import annotations

import time

from .backends.sim import SimulatedHF2
from .config import Config
from .lockin import LockIn


def build_sim_system(cfg: Config | None = None, clock=time.monotonic,
                     seed: int | None = None) -> tuple[LockIn, SimulatedHF2]:
    """Return (lockin, sim_backend) wired together but NOT yet started."""
    cfg = cfg or Config()
    backend = SimulatedHF2(clock=clock, seed=seed)
    # Put the simulated signals at the frequencies the channels START on, so a
    # fresh simulator shows a signal. Tuning a channel away still makes it
    # vanish, as on the real instrument.
    backend.ext_ref_Hz = [cfg.ch1.frequency_Hz, cfg.ch2.frequency_Hz]
    return LockIn(backend, cfg, clock=clock), backend
