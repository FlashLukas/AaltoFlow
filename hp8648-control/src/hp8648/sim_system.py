"""Build a fully-simulated HP 8648D system in one call.

Wire the simulated backend into a SignalSource so the service (and tests) can
run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedHP8648
from .source import SignalSource


def build_sim_system(cfg: Config | None = None,
                     initial: dict | None = None) -> tuple[SignalSource, SimulatedHP8648]:
    """Return (source, sim_backend) wired together but NOT yet started.
    The caller (service / test / GUI) calls source.start().

    `initial` is the simulated box's state BEFORE connecting (see
    SimulatedHP8648.POWER_ON); start() adopts it without writing anything."""
    cfg = cfg or Config()
    backend = SimulatedHP8648(hardware=cfg.hardware, initial=initial)
    src = SignalSource(backend, cfg)
    return src, backend
