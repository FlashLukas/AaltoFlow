"""End-to-end over ZeroMQ on loopback, on NON-default ports so the tests never
collide with a service Lukas has running."""

import time

import pytest

from hf2.config import Config
from hf2.sim_system import build_sim_system
from hf2.net.service import Hf2Service
from hf2.net.client import Hf2Client

CMD_PORT = 15890
PUB_PORT = 15891


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.ch1.time_constant_s = 2e-3        # short, so acquisitions finish fast
    cfg.ch2.time_constant_s = 2e-3
    li, sim = build_sim_system(cfg, seed=3)
    svc = Hf2Service(li, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                     status_hz=20.0)
    svc.start()
    cli = Hf2Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                       # let PUB/SUB connect
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.03)
    return pred()


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["tc_max_s"] == Config().limits.tc_max_s
    assert [c["demod"] for c in info["channels"]] == [0, 3]
    assert cli.get_config().hardware.server_port == 8005


def test_settings_take_effect(service_and_client):
    _, cli, _ = service_and_client
    assert cli.set_time_constant(1, 0.05)["ok"]
    assert cli.set_order(2, 2)["ok"]
    assert cli.set_reference(1, "internal")["ok"]
    assert cli.set_frequency(1, 999.0)["ok"]
    s = _wait(lambda: (lambda st: st if st.tc_set_s[0] == 0.05 and st.order[1] == 2
                       and st.freq_set_Hz[0] == 999.0 else None)(cli.status()))
    assert s is not None
    assert s.reference[0] == "internal"


def test_refusals_come_back_as_errors(service_and_client):
    _, cli, _ = service_and_client
    assert cli.set_reference(2, "external")["ok"]
    r = cli.set_frequency(2, 1000.0)          # refused: the PLL owns ch2's frequency now
    assert r["ok"] is False and "EXTERNAL" in r["error"]
    r = cli._cmd({"cmd": "set_order", "channel": 5, "order": 2})
    assert r["ok"] is False
    assert cli._cmd({"cmd": "no_such_verb"})["ok"] is False


def test_acquire_blocking_returns_its_own_sample(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    first = cli.acquire_blocking(timeout_s=5.0)
    second = cli.acquire_blocking(timeout_s=5.0)
    assert second["acq_id"] == first["acq_id"] + 1
    assert second["r"][0] > 0
    assert len(second["aux_in"]) == 2


def test_status_is_valid_json_before_any_reading(service_and_client):
    """NaN is not JSON. Missing readings must travel as null, and the reply
    must parse at all (a strict parser rejects the NaN token)."""
    import json
    svc, cli, _ = service_and_client
    raw = json.dumps(svc.status_payload(), allow_nan=False)   # raises on NaN
    assert "sample" in json.loads(raw)


def test_set_config_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.cfg.limits.tc_max_s = 0.1
    cli.cfg.ch1.time_constant_s = 5.0
    cli.apply_config()
    s = _wait(lambda: (lambda st: st if st.tc_set_s[0] == 0.1 else None)(cli.status()))
    assert s is not None
