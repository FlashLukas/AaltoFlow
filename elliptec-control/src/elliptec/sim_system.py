"""Wire up a simulated (or real) Elliptec system.

``build_sim_system(cfg)`` returns a ``(brain, backend)`` pair that is fully
wired but NOT started -- the caller decides when to ``brain.start()``.  Every
entry point (service, GUI, smoke test, tests) uses this so they all get an
identical, hardware-free stack.
"""

from __future__ import annotations

from .backends.sim import SimEllBus
from .config import Config
from .mount import RotationMount


def build_sim_system(cfg: Config | None = None) -> tuple[RotationMount, SimEllBus]:
    cfg = cfg or Config()
    backend = SimEllBus(cfg)
    return RotationMount(backend, cfg), backend


def build_real_system(cfg: Config | None = None):
    """Wire up the REAL serial bus (the driver module is imported lazily, so
    importing the simulator path never drags pyserial in)."""
    from .backends.ell_serial import EllSerialBus

    cfg = cfg or Config()
    backend = EllSerialBus(cfg)
    return RotationMount(backend, cfg), backend
