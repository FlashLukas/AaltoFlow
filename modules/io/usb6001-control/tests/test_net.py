"""End-to-end over ZeroMQ: a service (simulated card) and a client on loopback.
Non-default ports 17750/17751, so it never collides with a running service."""

import math
import time

import pytest

from usb6001.net.client import Usb6001Client
from usb6001.net.service import Usb6001Service
from usb6001.sim_system import build_sim_system, demo_config

CMD_PORT = 17750
PUB_PORT = 17751


@pytest.fixture
def service_and_client():
    daq, sim = build_sim_system(demo_config())
    svc = Usb6001Service(daq, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                         status_hz=20.0)
    svc.start()
    cli = Usb6001Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=3000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(cli, cond, n=40):
    s = cli.status()
    for _ in range(n):
        s = cli.status()
        if cond(s):
            break
        time.sleep(0.05)
    return s


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["do"] == ["p0.4", "p0.5", "p0.6", "p0.7"]
    assert info["ai"] == ["ai0", "ai1", "ai2", "ai3"]
    assert cli.get_config().ai.channels[2].unit == "mT"
    assert cli.cfg.dio.lines[4].direction == "out"


def test_ao_unknown_until_set_then_echoed(service_and_client):
    _, cli, sim = service_and_client
    s = cli.status()
    assert s.ao_known == [False, False] and math.isnan(s.ao_V[0])
    assert cli.set_ao(0, 1.25) == 1.25
    s = _wait(cli, lambda s: s.ao_known[0])
    assert s.ao_V[0] == 1.25 and sim.writes == [("ao", 0, 1.25)]


def test_clamp_and_refusal_over_wire(service_and_client):
    _, cli, _ = service_and_client
    assert cli.set_ao("ao1", 99.0) == 5.0
    with pytest.raises(ValueError, match="configured as 'in'"):
        cli.set_do("p0.0", True)
    assert cli.set_do("p0.4", True) is True
    s = _wait(cli, lambda s: s.dio[4] is True)
    assert s.dio[4] is True and s.dio_dir[4] == "out"


def test_fresh_reads_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.set_ao(0, -0.75)
    r = cli.read_ai("ai0")
    assert abs(r["values_V"]["ai0"] + 0.75) < 0.01
    d = cli.read_di()
    assert set(d["levels"]) == {"p0.0", "p0.1", "p0.2", "p0.3", "p2.0"}
    a = cli.acquire()
    s = _wait(cli, lambda s: s.sample.get("acq_id") == a and not s.acquiring)
    assert s.sample["acq_id"] == a


def test_describe_and_rev(service_and_client):
    _, cli, _ = service_and_client
    m = cli.describe()
    ids = {p["id"] for p in m["parameters"]}
    assert "do_p0_4" in ids and "ai0" in ids
    s = _wait(cli, lambda s: s.describe_rev is not None)
    assert s.describe_rev == m["revision"]


def test_set_config_over_wire_marks_restart(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.cfg.ao.channels[0].max_V = 1.0
    cli.cfg.dio.lines[0].direction = "out"
    cli.apply_config()
    assert cli.set_ao(0, 3.0) == 1.0                 # the new limit applies at once
    s = _wait(cli, lambda s: s.restart_pending)
    assert s.restart_pending is True and s.dio_dir[0] == "in"   # the direction does not
