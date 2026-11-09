"""Shared test helpers.

The src-layout shim lets a bare `pytest` find `alr3203` straight from src/.
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

from alr3203.backends.sim import SimRelay, SimulatedALR3203
from alr3203.config import Config
from alr3203.supply import BipolarSupply


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class Spy:
    """Wraps the simulated supply and records every call in order."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **kw):
            # keyword arguments (close(output_off=...)) recorded as a dict
            self.calls.append((name,) + a + ((kw,) if kw else ()))
            return attr(*a, **kw)
        return wrapped


def make_rig(cfg=None, relay_inverted=False):
    """(supply, spy, clock, events, relay), worker NOT running: call step().
    The relay is a SimRelay that knows the simulated supply, so it counts a
    switch under current (`relay.hot_switches`)."""
    cfg = cfg or Config()
    clock = FakeClock()
    sim = SimulatedALR3203(load=cfg.sim, clock=clock, seed=0)
    spy = Spy(sim)
    relay = SimRelay(sim, inverted=relay_inverted)
    supply = BipolarSupply(spy, cfg, relay=relay, clock=clock)
    events = []
    supply._on_event = lambda lvl, msg: events.append((lvl, msg))
    supply.start(poll=False)
    supply.step()
    return supply, spy, clock, events, relay


@pytest.fixture
def rig():
    """(supply, spy, clock, events) with the worker NOT running: call step()."""
    supply, spy, clock, events, _relay = make_rig()
    yield supply, spy, clock, events
    supply.shutdown()


def run_for(supply, clock, seconds, dt=0.05):
    """Advance the fake clock in worker-sized steps, stepping the brain."""
    n = int(round(seconds / dt))
    for _ in range(n):
        clock.advance(dt)
        supply.step()
