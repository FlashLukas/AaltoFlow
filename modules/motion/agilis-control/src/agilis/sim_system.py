"""Wire up a simulated or real Agilis system (section 2 of the guide).

``build_sim_system(cfg)`` returns a ``(brain, backend)`` pair that is fully
wired but NOT started -- the caller decides when to ``brain.start()``. Every
entry point (service, GUI, smoke test, tests) uses this so they all get an
identical, hardware-free stack.
"""

from __future__ import annotations

from .agilis import AgilisStage
from .backends.sim import SimAgilis
from .config import Config


def build_sim_system(cfg: Config | None = None) -> tuple[AgilisStage, SimAgilis]:
    cfg = cfg or Config()
    backend = SimAgilis(cfg)
    return AgilisStage(backend, cfg), backend


def build_real_system(cfg: Config | None = None):
    """Wire up the REAL AG-UC2 stack (imported lazily).

    Kept out of :func:`build_sim_system` so importing the simulator path never
    drags in the hardware module.
    """
    from .backends.ag_uc2 import AgUC2

    cfg = cfg or Config()
    backend = AgUC2(cfg)
    return AgilisStage(backend, cfg), backend
