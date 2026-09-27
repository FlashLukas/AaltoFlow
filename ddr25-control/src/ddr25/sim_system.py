"""Wire up a rotation-stage system (section 2 of the guide).

``build_sim_system(cfg)`` returns a ``(brain, backend)`` pair that is fully
wired but NOT started -- the caller decides when to ``brain.start()``. Every
entry point (service, GUI, smoke test, tests) uses this so they all get an
identical, hardware-free stack.
"""

from __future__ import annotations

from .backends.sim import SimRotator
from .config import Config
from .rotator import Rotator


def build_sim_system(cfg: Config | None = None) -> tuple[Rotator, SimRotator]:
    cfg = cfg or Config()
    backend = SimRotator(cfg)
    return Rotator(backend, cfg), backend


def build_real_system(cfg: Config | None = None):
    """The REAL K-Cube + DDR25 stack (the driver module is imported lazily,
    so importing this file never drags in pylablib)."""
    from .backends.kinesis import KinesisRotator

    cfg = cfg or Config()
    backend = KinesisRotator(cfg)
    return Rotator(backend, cfg), backend
