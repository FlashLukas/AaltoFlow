"""Build a Controller wired to the simulated vector magnet.

Kept free of any Qt import so the headless service and the tests can use it.
"""

from __future__ import annotations

import time

from .backends.sim import SimVectorMagnet
from .config import Config
from .controller import Controller


def build_sim_system(cfg: Config | None = None, *, clock=None, sleep=None,
                     seed: int | None = None):
    """Return (controller, sim), not started.

    `clock`/`sleep`: pass a backends.sim.FakeClock (and its .sleep) to run the
    magnet on simulated time; default is the real clock.
    """
    cfg = cfg or Config()
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    sim = SimVectorMagnet(cfg.sim, cfg.hall, cfg.temperature,
                          ai_range=(cfg.hardware.ai_min_V, cfg.hardware.ai_max_V),
                          clock=clock, seed=seed)
    ctrl = Controller(sim, cfg, clock=clock, sleep=sleep)
    return ctrl, sim
