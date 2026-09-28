"""Build a fully-simulated gaussmeter in one call. Qt-free on purpose."""

from __future__ import annotations

from .backends.sim import SimulatedLS455
from .config import Config
from .gaussmeter import Gaussmeter


def build_sim_system(cfg: Config | None = None, realtime: bool = True,
                     **sim_kwargs) -> tuple[Gaussmeter, SimulatedLS455]:
    """Return (gaussmeter, sim_backend) wired together but NOT yet started.

    realtime=True makes each simulated reading take as long as on the real 455
    (30 readings/s, 10 at 5 digits) -- right for the GUI and the service; tests
    pass False to run fast.
    """
    cfg = cfg or Config()
    sim_kwargs.setdefault("realtime", realtime)
    backend = SimulatedLS455(**sim_kwargs)
    return Gaussmeter(backend, cfg), backend
