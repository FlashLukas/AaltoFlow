"""Build a fully-simulated analyser in one call. Qt-free on purpose."""

from __future__ import annotations

from .backends.sim import SimulatedAnalyzer
from .config import Config
from .spectrum import SpectrumAnalyzer


def build_sim_system(cfg: Config | None = None, realtime: bool = True,
                     seed: int | None = None) -> tuple[SpectrumAnalyzer, SimulatedAnalyzer]:
    """Return (analyzer, sim_backend) wired together but NOT yet started.

    realtime=True makes sweeps take as long as on the real analyser (right for
    the GUI and the service); tests pass False to run instantly.
    """
    cfg = cfg or Config()
    backend = SimulatedAnalyzer(cfg, seed=seed, time_scale=1.0 if realtime else 0.0)
    return SpectrumAnalyzer(backend, cfg), backend
