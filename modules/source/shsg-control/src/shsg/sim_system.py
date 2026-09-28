"""Build a generator system in one call: simulated, or the real TG through the
signalhound service. Qt-free on purpose (the service and the tests use it)."""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedTG44A
from .generator import Generator


def build_sim_system(cfg: Config | None = None) -> tuple[Generator, SimulatedTG44A]:
    """Return (generator, sim_backend) wired together but NOT yet started.
    The caller (service / test) calls generator.start()."""
    cfg = cfg or Config()
    backend = SimulatedTG44A(startup=cfg.signal)
    return Generator(backend, cfg), backend


def build_real_system(cfg: Config | None = None):
    """Return (generator, RemoteTG): the real TG44A, reached as a CLIENT of the
    signalhound service at cfg.hardware.owner_host / owner_*_port."""
    from .backends.remote_sa import RemoteTG
    cfg = cfg or Config()
    hw = cfg.hardware
    backend = RemoteTG(hw.owner_host, hw.owner_cmd_port, hw.owner_pub_port,
                       timeout_ms=hw.owner_timeout_ms, wait_s=hw.owner_wait_s)
    return Generator(backend, cfg), backend
