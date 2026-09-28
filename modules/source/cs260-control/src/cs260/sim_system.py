"""Build a fully-simulated monochromator in one call.

Wires the simulated backend into a Monochromator so the service (and tests) can
run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

import time

from .config import Config
from .backends.sim import SimulatedCS260
from .monochromator import Monochromator


def build_sim_system(cfg: Config | None = None,
                     clock=time.monotonic) -> tuple[Monochromator, SimulatedCS260]:
    """Return (monochromator, sim_backend) wired together but NOT yet started.
    The caller (service / test / GUI) calls monochromator.start()."""
    cfg = cfg or Config()
    backend = SimulatedCS260(cfg, clock=clock)
    mono = Monochromator(backend, cfg, clock=clock)
    return mono, backend
