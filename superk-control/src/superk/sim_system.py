"""Build a fully-simulated laser system in one call.

Wire the simulated backend into a SuperK brain so the service (and the tests)
can run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedSuperK
from .laser import SuperK


def build_sim_system(cfg: Config | None = None) -> tuple[SuperK, SimulatedSuperK]:
    """Return (laser, sim_backend) wired together but NOT yet started.
    The caller (service / test) calls laser.start()."""
    cfg = cfg or Config()
    backend = SimulatedSuperK(warmup_s=cfg.hardware.sim_warmup_s)
    laser = SuperK(backend, cfg)
    return laser, backend
