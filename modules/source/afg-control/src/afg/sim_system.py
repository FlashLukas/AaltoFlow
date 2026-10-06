"""Build a fully-simulated generator in one call.

Wires the simulated AFG1062 into a Generator so the service (and the tests)
can run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedAFG
from .generator import Generator


def build_sim_system(cfg: Config | None = None, boot: dict | None = None
                     ) -> tuple[Generator, SimulatedAFG]:
    """Return (generator, sim_backend) wired together but NOT yet started.
    The caller (service / test) calls generator.start().

    `boot`: what the pretend AFG is doing before the service starts (default
    backends.sim.SIM_BOOT_STATE: CH1 driving a 30 Hz sine). The service adopts
    it -- it never pushes the config at start."""
    cfg = cfg or Config()
    backend = SimulatedAFG(boot=boot)
    return Generator(backend, cfg), backend
