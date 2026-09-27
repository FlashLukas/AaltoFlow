"""Build a fully-simulated supply in one call.

Wires the simulated BOP (with its coil load) into a BipolarSupply so the
service, the GUI and the tests run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedBOP
from .supply import BipolarSupply


def build_sim_system(cfg: Config | None = None, seed=None) -> tuple[BipolarSupply, SimulatedBOP]:
    """Return (supply, sim_backend) wired together but NOT yet started.
    The caller (service / test / GUI) calls supply.start()."""
    cfg = cfg or Config()
    backend = SimulatedBOP(load=cfg.sim, seed=seed)
    supply = BipolarSupply(backend, cfg)
    return supply, backend
