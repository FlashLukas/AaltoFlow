"""Build a fully-simulated power meter in one call. Qt-free on purpose."""

from __future__ import annotations

from .backends.sim import SimulatedPM16
from .config import Config
from .meter import PowerMeter


def build_sim_system(cfg: Config | None = None, realtime: bool = True,
                     **sim_kwargs) -> tuple[PowerMeter, SimulatedPM16]:
    """Return (meter, sim_backend) wired together but NOT yet started.

    realtime=True makes each simulated reading take 60 ms like the real PM16
    (right for the GUI and the service); tests pass False to run fast.
    """
    cfg = cfg or Config()
    sim_kwargs.setdefault("sample_period_s", 0.06 if realtime else 0.0)
    backend = SimulatedPM16(**sim_kwargs)
    return PowerMeter(backend, cfg), backend
