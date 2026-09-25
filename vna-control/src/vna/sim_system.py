"""Build a fully-simulated VNA in one call. Qt-free on purpose."""

from __future__ import annotations

from .analyzer import Analyzer
from .backends.sim import SimulatedVna
from .config import Config


def build_sim_system(cfg: Config | None = None, realtime: bool = True,
                     seed: int | None = None) -> tuple[Analyzer, SimulatedVna]:
    """Return (analyzer, sim_backend) wired together but NOT yet started.

    realtime=True makes sweeps take as long as on a real analyser (right for the
    GUI and the service); tests pass False to run instantly.
    """
    cfg = cfg or Config()
    backend = SimulatedVna(cfg, seed=seed, time_scale=1.0 if realtime else 0.0)
    return Analyzer(backend, cfg), backend
