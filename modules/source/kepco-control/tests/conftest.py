"""Shared test helpers.

The src-layout shim lets a bare `pytest` find `kepco` straight from src/.
`FakeClock` + `Spy` let the brain be stepped by hand, deterministically:
the brain AND the simulated load read the same fake clock, so a test can say
"0.1 s passes" and check exactly what the ramp did in that time.
"""

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Tests of the security itself set their
# own folder.
import os as _os
import tempfile as _tempfile
_os.environ["AALTOFLOW_SECURITY_DIR"] = _tempfile.mkdtemp(prefix="aaltoflow-nosec-")


import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

import pytest

from kepco.backends.sim import SimulatedBOP
from kepco.config import Config
from kepco.supply import BipolarSupply


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class Spy:
    """Wraps the simulated BOP and records every call in order."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def wrapped(*a):
            self.calls.append((name,) + a)
            return attr(*a)
        return wrapped


@pytest.fixture
def rig():
    """(supply, spy, clock, events) with the worker NOT running: call step()."""
    cfg = Config()
    clock = FakeClock()
    sim = SimulatedBOP(load=cfg.sim, clock=clock, seed=0)
    spy = Spy(sim)
    supply = BipolarSupply(spy, cfg, clock=clock)
    events = []
    supply._on_event = lambda lvl, msg: events.append((lvl, msg))
    supply.start(poll=False)
    supply.step()
    yield supply, spy, clock, events
    supply.shutdown()


def run_for(supply, clock, seconds, dt=0.05):
    """Advance the fake clock in worker-sized steps, stepping the brain."""
    n = int(round(seconds / dt))
    for _ in range(n):
        clock.advance(dt)
        supply.step()
