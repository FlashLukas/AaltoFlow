"""Wire up a simulated / real Z-piezo system (blueprint §2)."""

from __future__ import annotations

from .backends.sim import SimZ
from .config import Config
from .zpiezo import ZPiezo


def build_sim_system(cfg: Config | None = None):
    cfg = cfg or Config()
    backend = SimZ(v0=cfg.limits.v_min, v_min=cfg.limits.v_min, v_max=cfg.limits.v_max)
    brain = ZPiezo(backend, cfg)
    return brain, backend


def build_real_system(cfg: Config | None = None):
    from .backends.kcube import KCubeZ
    cfg = cfg or Config()
    backend = KCubeZ(cfg.hardware.serial, cfg.limits.v_min, cfg.limits.v_max)
    brain = ZPiezo(backend, cfg)
    return brain, backend
