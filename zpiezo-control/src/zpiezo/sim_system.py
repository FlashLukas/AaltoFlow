"""Wire up a simulated / real Z-piezo system (blueprint §2)."""

from __future__ import annotations

from .backends.sim import SimZ
from .config import Config
from .zpiezo import ZPiezo


def build_sim_system(cfg: Config | None = None, v0: float | None = None):
    """``v0`` = the voltage the simulated KCube is ALREADY holding when the
    service starts (tests set a non-default value to prove it is adopted,
    not overwritten).  Default: the bottom of the envelope."""
    cfg = cfg or Config()
    v0 = cfg.limits.v_min if v0 is None else float(v0)
    backend = SimZ(v0=v0, v_min=cfg.limits.v_min, v_max=cfg.limits.v_max)
    brain = ZPiezo(backend, cfg)
    return brain, backend


def build_real_system(cfg: Config | None = None):
    from .backends.kcube import KCubeZ
    cfg = cfg or Config()
    backend = KCubeZ(cfg.hardware.serial, cfg.limits.v_min, cfg.limits.v_max)
    brain = ZPiezo(backend, cfg)
    return brain, backend
