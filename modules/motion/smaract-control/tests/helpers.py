"""Shared test helpers: a fast simulated positioner and wait loops."""

import time

from smaract.config import Config
from smaract.sim_system import build_sim_system


def fast_cfg() -> Config:
    """Defaults, but allowed to travel fast so tests do not crawl."""
    cfg = Config()
    cfg.limits.max_velocity_mm_s = 18.0
    cfg.motion.velocity_mm_s = 18.0
    return cfg


def make_brain(cfg=None, start=True, **sim):
    cfg = cfg or fast_cfg()
    # The brain ADOPTS the controller's speed at start and never pushes the
    # config value (2026-09-27), so the fast test speed has to be the state
    # the simulated controller is already in.
    hw = cfg.hardware
    sim.setdefault("freq_hz", int(round(cfg.motion.velocity_mm_s / (hw.um_per_step * 1e-3))))
    brain, backend = build_sim_system(cfg, **sim)
    if start:
        brain.start()
    return brain, backend


def wait_until(pred, timeout=10.0, dt=0.01):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(dt)
    return False


def wait_idle(brain, timeout=15.0):
    time.sleep(0.05)
    return wait_until(lambda: not brain.status().moving, timeout)


def referenced_brain(cfg=None, **sim):
    brain, backend = make_brain(cfg, **sim)
    brain.find_reference()
    assert wait_until(lambda: brain.status().referenced and not brain.status().referencing,
                      15.0), "reference search did not finish"
    return brain, backend
