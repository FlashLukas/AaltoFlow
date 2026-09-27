"""Build a fully simulated SMU system in one call.

Wire the simulated 2450 (with its pretend sample) into a SourceMeter so the
service, the GUI and the tests run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .backends.sim import SimulatedK2450
from .config import Config
from .smu import SourceMeter


def build_sim_system(cfg: Config | None = None, realtime: bool = True,
                     seed: int | None = None) -> tuple[SourceMeter, SimulatedK2450]:
    """Return (smu, sim_backend) wired together but NOT yet started.
    The caller (service / test / GUI) calls smu.start().

    realtime=False skips the simulated integration time (fast unit tests)."""
    cfg = cfg or Config()
    backend = SimulatedK2450(sim=cfg.sim, line_freq_Hz=cfg.hardware.line_freq_Hz,
                             seed=seed, realtime=realtime)
    return SourceMeter(backend, cfg), backend
