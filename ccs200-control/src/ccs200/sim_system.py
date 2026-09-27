"""Build a fully-simulated spectrometer in one call. Qt-free on purpose."""

from __future__ import annotations

from .backends.sim import SimulatedSpectrometer
from .config import Config
from .spectrometer import Spectrometer


def build_sim_system(cfg: Config | None = None, realtime: bool = True,
                     seed: int | None = None,
                     integration_s: float | None = None) -> tuple[Spectrometer, SimulatedSpectrometer]:
    """Return (spectrometer, sim_backend) wired together but NOT yet started.

    realtime=True makes scans take as long as on the real instrument (right for
    the GUI and the service); tests pass False to run instantly.
    `integration_s` is the simulated instrument's PRE-EXISTING integration
    time, which the brain adopts at start (None = the sim's own 5 ms, which
    is deliberately not the config default of 10 ms).
    """
    cfg = cfg or Config()
    backend = SimulatedSpectrometer(cfg, seed=seed, time_scale=1.0 if realtime else 0.0,
                                     integration_s=integration_s)
    return Spectrometer(backend, cfg), backend
