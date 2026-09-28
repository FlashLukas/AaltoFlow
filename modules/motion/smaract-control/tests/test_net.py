"""Service <-> client round-trip on NON-default ports (section 9).

Ports 17180/17181 are reserved for this module's tests, so they never collide
with a running service or with another module's tests.
"""

import time

import pytest

from helpers import fast_cfg, wait_until
from smaract.net.client import SmaractClient
from smaract.net.service import SmaractService
from smaract.sim_system import build_sim_system

CMD, PUB = 17180, 17181


@pytest.fixture()
def service_and_client():
    brain, _ = build_sim_system(fast_cfg(), power_on_mm=38.0)
    svc = SmaractService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = SmaractClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    time.sleep(0.2)  # let PUB warm up (SUB is a slow joiner)
    try:
        yield brain, cli
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)


def _reference(cli):
    rid = cli.find_reference()
    assert wait_until(lambda: cli.status().ref_id == rid and cli.status().referenced
                      and not cli.status().referencing, 15.0)


def test_info_and_config(service_and_client):
    _brain, cli = service_and_client
    info = cli.info()
    assert info["units"]["position"] == "mm"
    assert info["velocity_range"][0] < info["velocity_range"][1]
    cfg = cli.get_config()
    assert {"motion", "limits", "relative", "hardware", "ui"} <= set(cfg)


def test_errors_come_back_as_ok_false(service_and_client):
    _brain, cli = service_and_client
    with pytest.raises(RuntimeError, match="NOT referenced"):
        cli.move_to(10.0)                  # {"ok": false} -> client raises
    with pytest.raises(RuntimeError, match="unknown command"):
        cli._rpc(cmd="fly_to_the_moon")
    # and the service is still alive afterwards
    assert cli.info()["n_slots"] == 20


def test_reference_move_and_settle_over_wire(service_and_client):
    _brain, cli = service_and_client
    _reference(cli)
    target = cli.move_to(45.0)
    assert target == 45.0
    # the settle rule scan-core uses: target adopted, THEN not moving
    assert wait_until(lambda: cli.status().target_mm == 45.0 and not cli.status().moving, 10.0)
    st = cli.status()
    assert st.on_target and abs(st.position_mm - 45.0) < 1e-3


def test_clamped_over_wire_and_velocity(service_and_client):
    _brain, cli = service_and_client
    _reference(cli)
    assert cli.move_to(999.0) == 115.0
    cli.stop()
    assert cli.set_velocity(1000.0) == 18.0
    assert wait_until(lambda: cli.status().velocity_mm_s == 18.0)
    assert cli.set_hold_time(99999) == 60000


def test_zero_and_positions_over_wire(service_and_client):
    _brain, cli = service_and_client
    _reference(cli)
    cli.move_to(42.0)
    assert wait_until(lambda: cli.status().target_mm == 42.0 and not cli.status().moving, 10.0)
    origin = cli.set_zero()
    assert abs(origin - 42.0) < 1e-3
    cli.move_from_zero(1.5)
    assert wait_until(lambda: abs(cli.status().relative_mm - 1.5) < 1e-3
                      and not cli.status().moving, 10.0)
    cli.store_position(0, "wire")
    positions = cli.get_positions()
    assert positions[0]["used"] and positions[0]["name"] == "wire"
    assert abs(positions[0]["position_mm"] - 43.5) < 2e-3


def test_stream_over_wire(service_and_client):
    _brain, cli = service_and_client
    sid = cli.stream_start()
    cli.move_by(0.3)
    time.sleep(0.3)
    chunk = cli.stream_read()
    assert chunk["id"] == sid and len(chunk["t"]) > 3
    assert len(chunk["values"]["position"]) == len(chunk["t"])
    assert "now" in chunk
    cli.stream_stop()


def test_json_status_has_no_nan(service_and_client):
    """NaN is not JSON: before the first reading, values travel as null."""
    _brain, cli = service_and_client
    st = cli._rpc(cmd="status")["status"]
    assert isinstance(st["describe_rev"], int)
    for v in st.values():
        assert v != "NaN"
