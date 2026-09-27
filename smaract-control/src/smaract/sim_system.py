"""Wire up a positioner system (section 2 of the guide).

``build_sim_system(cfg)`` returns a ``(brain, backend)`` pair that is fully
wired but NOT started -- the caller decides when to ``brain.start()``. Every
entry point (service, GUI, smoke test, tests) uses this, so they all get an
identical, hardware-free stack. Keyword arguments go to the simulator (tests
use them to place the carriage near a reference mark, so referencing is quick).
"""

from __future__ import annotations

from .backends.sim import SimScu
from .config import Config
from .smaract import Positioner


def build_sim_system(cfg: Config | None = None, **sim_kwargs) -> tuple[Positioner, SimScu]:
    cfg = cfg or Config()
    backend = SimScu(cfg, **sim_kwargs)
    return Positioner(backend, cfg), backend


def build_real_system(cfg: Config | None = None):
    """Wire up the REAL SCU stack. The backend loads the SmarAct DLL only in
    open(), so importing this never needs the vendor software."""
    from .backends.scu import ScuStage

    cfg = cfg or Config()
    backend = ScuStage(cfg)
    return Positioner(backend, cfg), backend
