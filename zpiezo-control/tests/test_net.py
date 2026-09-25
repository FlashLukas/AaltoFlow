"""Service <-> client round-trip on NON-default ports."""

import time

import pytest

from zpiezo.config import Config
from zpiezo.net.client import ZPiezoClient
from zpiezo.net.service import ZPiezoService
from zpiezo.sim_system import build_sim_system

CMD, PUB = 15665, 15666


def _wait(cond, timeout=4.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


@pytest.fixture
def wired():
    brain, _ = build_sim_system(Config())
    svc = ZPiezoService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = ZPiezoClient("127.0.0.1", CMD, PUB)
    cli.start()
    yield cli
    cli.close()
    svc.stop()


def test_set_read_over_wire(wired):
    cli = wired
    assert cli.set_voltage(7.5) == 7.5
    assert cli.read_voltage() == 7.5


def test_clamp_over_wire(wired):
    cli = wired
    assert cli.set_voltage(999) == 75.0


def test_status_pub_arrives(wired):
    cli = wired
    cli.set_voltage(5.0)
    assert _wait(lambda: abs(cli.status().voltage - 5.0) < 1e-6)


def test_info(wired):
    info = wired.info()
    assert "idn" in info and info["limits"]["v_max"] == 75.0
