"""Build a fully-simulated PM400 in one call. Qt-free on purpose."""

from __future__ import annotations

from .backends.sim import SimulatedPM400
from .config import Config
from .meter import Pm400Meter


def build_sim_system(cfg: Config | None = None, realtime: bool = True,
                     **sim_kwargs) -> tuple[Pm400Meter, SimulatedPM400]:
    """Return (meter, sim_backend) wired together but NOT yet started.

    The simulator reads `cfg.sim` LIVE (which head is plugged in, the light on
    it). realtime=True makes each reading take its averaging time (or wait for
    the next pulse) like the real console -- right for the GUI and the service;
    tests pass False to run fast.
    """
    cfg = cfg or Config()
    backend = SimulatedPM400(cfg.sim, realtime=realtime, **sim_kwargs)
    return Pm400Meter(backend, cfg), backend
