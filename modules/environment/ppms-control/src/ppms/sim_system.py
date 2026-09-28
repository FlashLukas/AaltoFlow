"""Build a fully-simulated cryostat in one call.

Wires the simulated DynaCool into a Cryostat brain so the service (and tests)
run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .backends.sim import SimulatedDynaCool
from .config import Config
from .cryostat import Cryostat


def build_sim_system(cfg: Config | None = None, **sim_kwargs) -> tuple[Cryostat, SimulatedDynaCool]:
    """Return (cryostat, sim_backend) wired together but NOT yet started.
    The caller (service / test / GUI) calls cryostat.start()."""
    cfg = cfg or Config()
    backend = SimulatedDynaCool(**sim_kwargs)
    return Cryostat(backend, cfg), backend
