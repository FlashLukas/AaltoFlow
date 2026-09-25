"""Wire up a simulated piezo system (§2 of the guide).

``build_sim_system(cfg)`` returns a ``(brain, backend)`` pair that is fully
wired but NOT started -- the caller decides when to ``brain.start()``.  Every
entry point (service, GUI, smoke test, tests) uses this so they all get an
identical, hardware-free stack.
"""

from __future__ import annotations

from .backends.sim import SimPiezo
from .config import Config
from .piezo import Piezo


def build_sim_system(cfg: Config | None = None) -> tuple[Piezo, SimPiezo]:
    cfg = cfg or Config()
    backend = SimPiezo(cfg)
    brain = Piezo(backend, cfg)
    return brain, backend


def build_real_system(cfg: Config | None = None):
    """Wire up the REAL d-Drive stack (imported lazily).

    Kept out of :func:`build_sim_system` so importing the simulator path never
    drags in pyserial.
    """
    from .backends.ddrive import DDrivePiezo

    cfg = cfg or Config()
    backend = DDrivePiezo(cfg)
    brain = Piezo(backend, cfg)
    return brain, backend
