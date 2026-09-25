"""Wire up a simulated KIM101 system (§2 of the guide).

``build_sim_system(cfg)`` returns a ``(brain, backend)`` pair that is fully
wired but NOT started -- the caller decides when to ``brain.start()``.  Every
entry point (service, GUI, smoke test, tests) uses this so they all get an
identical, hardware-free stack.
"""

from __future__ import annotations

from .backends.sim import SimKim
from .config import Config
from .kim import Kim


def build_sim_system(cfg: Config | None = None) -> tuple[Kim, SimKim]:
    cfg = cfg or Config()
    backend = SimKim(cfg)
    brain = Kim(backend, cfg)
    return brain, backend


def build_real_system(cfg: Config | None = None):
    """Wire up the REAL Thorlabs KIM101 stack (imported lazily).

    Kept out of :func:`build_sim_system` so importing the simulator path never
    drags in the hardware module.
    """
    from .backends.kinesis_kim import KinesisKim

    cfg = cfg or Config()
    backend = KinesisKim(cfg)
    brain = Kim(backend, cfg)
    return brain, backend
