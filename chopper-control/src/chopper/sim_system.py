"""Build a fully-simulated chopper system in one call.

Wire the simulated MC2000B into a Chopper brain so the service, the GUI and the
tests run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

import time

from .config import Config
from .backends.sim import SimulatedMC2000B
from .chopper import Chopper


def build_sim_system(cfg: Config | None = None, clock=time.monotonic,
                     seed=None) -> tuple[Chopper, SimulatedMC2000B]:
    """Return (chopper, sim_backend) wired together but NOT yet started.
    The caller (service / test / GUI) calls chopper.start().

    The simulator reads cfg.sim LIVE (tau, jitter, EXT REF IN), so a Settings
    change to the simulated physics applies at once."""
    cfg = cfg or Config()
    backend = SimulatedMC2000B(cfg.sim, clock=clock, seed=seed)
    return Chopper(backend, cfg, clock=clock, simulated=True), backend
