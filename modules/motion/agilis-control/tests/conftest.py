"""Make the src/ layout importable during tests without installing.

Network tests of this module use ports 17160..17179 only, so they never collide
with a running service or with sibling modules' tests.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture()
def fast_brain():
    """A started brain on a FAST simulated controller (short tests).

    The real AG-UC2 steps at a few hundred steps/s; tests would crawl, so the
    simulator's PR rate is raised and the poll runs at 50 Hz.
    """
    from agilis.config import Config
    from agilis.sim_system import build_sim_system

    cfg = Config()
    cfg.hardware.poll_hz = 50
    brain, sim = build_sim_system(cfg)
    sim.pr_rate = 20000.0
    brain.start()
    try:
        yield brain, sim
    finally:
        brain.shutdown()


def settle(brain, axis, timeout=5.0):
    """Wait until the axis is idle in a snapshot taken AFTER the command."""
    time.sleep(0.08)
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < timeout:
        time.sleep(0.02)
    time.sleep(0.06)
    return brain.status()
