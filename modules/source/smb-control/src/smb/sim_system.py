"""Build a fully-simulated generator system in one call.

Mirrors clMag.sim_system: wire the simulated backend into a Generator so the
service (and tests) can run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedSMB100A
from .generator import Generator


def build_sim_system(cfg: Config | None = None) -> tuple[Generator, SimulatedSMB100A]:
    """Return (generator, sim_backend) wired together but NOT yet started.
    The caller (service / test) calls generator.start()."""
    cfg = cfg or Config()
    backend = SimulatedSMB100A(startup=cfg.signal)
    gen = Generator(backend, cfg)
    return gen, backend
