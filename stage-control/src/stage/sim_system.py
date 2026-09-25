"""Wire up a simulated stage system (§2 of the guide).

``build_sim_system(cfg)`` returns a ``(brain, backend)`` pair that is fully
wired but NOT started -- the caller decides when to ``brain.start()``.  Every
entry point (service, GUI, smoke test, tests) uses this so they all get an
identical, hardware-free stack.
"""

from __future__ import annotations

from .backends.sim import SimStage
from .config import Config
from .stage import Stage


def build_sim_system(cfg: Config | None = None) -> tuple[Stage, SimStage]:
    cfg = cfg or Config()
    backend = SimStage(cfg)
    brain = Stage(backend, cfg)
    return brain, backend


def build_real_system(cfg: Config | None = None):
    """Wire up the REAL Thorlabs BSC203 stack (imported lazily).

    Kept out of :func:`build_sim_system` so importing the simulator path never
    drags in the hardware module.
    """
    from .backends.kinesis import KinesisStage

    cfg = cfg or Config()
    backend = KinesisStage(cfg)
    brain = Stage(backend, cfg)
    return brain, backend
