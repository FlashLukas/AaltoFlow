"""Adopt-on-start (Lukas, 2026-09-27): start() READS the KCube, writes nothing.

For a focus piezo the voltage the KCube already holds IS the focus, so a write
at start would defocus the sample.  These tests start the instrument from a
NON-default state (12.3 V) and prove it is adopted, not overwritten.
"""

import time

import pytest

from zpiezo.backends.sim import SimZ
from zpiezo.config import Config
from zpiezo.net.client import ZPiezoClient
from zpiezo.net.service import ZPiezoService
from zpiezo.sim_system import build_sim_system
from zpiezo.zpiezo import ZPiezo

V0 = 12.3


class RecordingZ(SimZ):
    """A sim KCube that fails the test on ANY state-changing call."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.writes = []
        self.armed = True

    def set_voltage(self, volts):
        self.writes.append(volts)
        if self.armed:
            raise AssertionError(f"state-changing write during start: {volts}")
        super().set_voltage(volts)


def test_open_issues_no_state_changing_writes():
    be = RecordingZ(v0=V0)
    brain = ZPiezo(be, Config())
    brain.start()                      # would raise on any set_voltage
    assert be.writes == []
    assert be.read_voltage() == V0


def test_status_after_start_reflects_instrument():
    brain, be = build_sim_system(Config(), v0=V0)
    brain.start()
    s = brain.status()
    assert s.voltage == pytest.approx(V0)
    assert s.target == pytest.approx(V0)   # adopted, not the old default 0
    brain.shutdown()


def test_unrelated_set_config_writes_nothing():
    be = RecordingZ(v0=V0)
    brain = ZPiezo(be, Config())
    brain.start()
    brain.set_config({"hardware": {"step_v": 0.5}})
    assert be.writes == [] and be.read_voltage() == V0


def test_narrowed_limits_still_reclamp():
    # An EXPLICIT set_config that moves the envelope below the held voltage
    # is a user request: the target is clamped into it (unchanged behaviour).
    be = RecordingZ(v0=V0)
    brain = ZPiezo(be, Config())
    brain.start()
    be.armed = False
    brain.set_config({"limits": {"v_max": 10.0}})
    assert be.read_voltage() == pytest.approx(10.0)


def test_out_of_envelope_voltage_adopted_as_is():
    be = RecordingZ(v0=80.0)                   # above v_max 75
    brain = ZPiezo(be, Config())
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    assert be.writes == [] and brain.status().target == pytest.approx(80.0)
    assert any(lvl == "warn" for lvl, _ in events)


def test_unreadable_instrument_writes_nothing():
    class Broken(RecordingZ):
        def read_voltage(self):
            raise RuntimeError("usb")
    be = Broken(v0=V0)
    brain = ZPiezo(be, Config())
    brain.start()
    assert be.writes == []


def test_service_publishes_adopted_voltage():
    brain, _ = build_sim_system(Config(), v0=V0)
    svc = ZPiezoService(brain, host="127.0.0.1", cmd_port=15667, pub_port=15668,
                        status_hz=20)
    svc.start()
    cli = ZPiezoClient("127.0.0.1", 15667, 15668)
    cli.start()
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 4 and abs(cli.status().voltage - V0) > 1e-9:
            time.sleep(0.02)
        assert cli.status().voltage == pytest.approx(V0)
        assert cli.read_voltage() == pytest.approx(V0)
    finally:
        cli.close()
        svc.stop()
