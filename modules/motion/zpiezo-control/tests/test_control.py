"""One controller, many viewers (src/zpiezo/control.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones are VIEWERS that cannot change anything, and control
changes hands only deliberately. The z piezo has no GUI of its own, but the
measurement suite's Control tab, scripts and the camera's autofocus all talk
to it. The service enforces the rule; the camera / scan-core ("machine")
bypass it; a script must take control. It has NO safety verb: nothing moves
after a set_voltage was written, and parking it would defocus the holder's
measurement (net/service.py). Control belongs to a PC, so every test client
sits at its OWN PC (identity host "user@pcN").

Wire tests use ports 18606/18607.
"""

from __future__ import annotations

import time

import pytest

from zpiezo.config import Config
from zpiezo.sim_system import build_sim_system

CMD, PUB = 18606, 18607
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from zpiezo.net.service import ZPiezoService
    brain, _ = build_sim_system(Config())
    s = ZPiezoService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    _wait_ports_free()


def _wait_ports_free(timeout=3.0):
    """The next test binds the same ports. ZeroMQ closes a socket in its own
    I/O thread, a moment AFTER close() returned, so wait until a test bind of
    both ports succeeds (otherwise that test fails with 'cannot listen')."""
    ctx = zmq.Context.instance()
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        s = ctx.socket(zmq.REP)
        s.setsockopt(zmq.LINGER, 0)
        try:
            s.bind(f"tcp://127.0.0.1:{CMD}")
            s.unbind(f"tcp://127.0.0.1:{CMD}")
            s.bind(f"tcp://127.0.0.1:{PUB}")
            s.close(0)
            time.sleep(0.05)             # let the test socket's own close finish
            return
        except zmq.ZMQError:
            s.close(0)
            time.sleep(0.05)


@pytest.fixture
def clients():
    from zpiezo.net.client import ZPiezoClient
    made = []

    def make(kind="gui", name="measurement suite", pc=None):
        c = ZPiezoClient("127.0.0.1", CMD, PUB, timeout_ms=3000, kind=kind, name=name)
        # control belongs to a PC: each test client sits at its OWN PC unless
        # the test says otherwise (all of them really run on this one)
        c.identity["host"] = f"user@{pc or f'pc{len(made)}'}"
        c.start()
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def _raw(req: dict) -> dict:
    """A request from something that does not say who it is (an old script)."""
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(f"tcp://127.0.0.1:{CMD}")
    try:
        s.send_json(req)
        return s.recv_json()
    finally:
        s.close(0)


def _wait(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_without_a_holder_everything_works_as_before(svc, clients):
    a = clients()
    assert a.set_voltage(5.0) == pytest.approx(5.0)
    assert _raw({"cmd": "set_voltage", "volts": 6.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from zpiezo.control import ControlRefused
    a = clients(name="suite A")
    b = clients(name="suite B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.set_voltage(4.0)
    with pytest.raises(ControlRefused, match="suite A"):
        b.set_voltage(9.0)
    with pytest.raises(ControlRefused):
        b.set_config({})                              # settings too
    r = _raw({"cmd": "set_voltage", "volts": 9.0})    # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    assert svc.brain.status().target == pytest.approx(4.0)   # the focus stayed
    # a viewer may always read
    assert b.read_voltage() == pytest.approx(4.0)
    assert "limits" in b.info()
    assert b.get_config()
    assert b.describe()["module"] == "zpiezo"
    # no safety verb: a viewer may send nothing that changes the focus
    assert svc.control.status()["always"] == ["shutdown"]


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from zpiezo.control import ControlRefused
    a = clients(name="suite A")
    a.take_control()
    cam = clients(kind="machine", name="camera")
    cam.set_voltage(3.0)                              # the camera's autofocus goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_voltage(2.0)
    assert script.take_control(force=True)            # deliberate, visible
    script.set_voltage(2.0)
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_voltage(1.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="suite A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="suite B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "suite B"
    assert {c["name"] for c in st["clients"]} >= {"suite A", "suite B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="suite A")
    b = clients(name="suite B")
    a.take_control()
    a.close()                                          # crashed client: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.set_voltage(2.5)                                 # free again
