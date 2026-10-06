"""Build a fully-simulated scope in one call (Qt-free), for the service and
the tests: the brain on a SimulatedScope watching the simulated bench."""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedScope
from .scope import Scope


def build_sim_system(cfg: Config | None = None, seed: int | None = None
                     ) -> tuple[Scope, SimulatedScope]:
    """(scope, sim_backend), wired but NOT started; the caller calls start().
    The simulator reads the live `cfg.sim` group, so set_sim / set_config
    change the pretend bench at once."""
    cfg = cfg or Config()
    backend = SimulatedScope(cfg.sim, seed=seed)
    return Scope(backend, cfg), backend
