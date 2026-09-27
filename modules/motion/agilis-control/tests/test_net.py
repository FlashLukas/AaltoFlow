"""Service <-> client round-trip on this module's test ports (17160..17169).

Covers the remote surface: moves in steps and um, jog, amplitude, step size,
leash, datum, positions, the stream verbs, errors as {"ok": false}.
"""

import time

import pytest

from agilis.config import Config
from agilis.net.client import AgilisClient
from agilis.net.service import AgilisService
from agilis.sim_system import build_sim_system

CMD, PUB = 17160, 17161


@pytest.fixture()
def service_and_client():
    cfg = Config()
    cfg.hardware.poll_hz = 50
    brain, sim = build_sim_system(cfg)
    sim.pr_rate = 20000.0
    svc = AgilisService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = AgilisClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    time.sleep(0.2)  # let PUB warm up
    try:
        yield brain, cli
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)


def _wait(cli, pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred(cli.status()):
            return True
        time.sleep(0.02)
    return False


def test_info_and_config(service_and_client):
    _brain, cli = service_and_client
    info = cli.info()
    assert info["axes"] == ["X", "Y"] and "SIM AG-UC2" in info["idn"]
    cfg = cli.get_config()
    assert set(cfg) == {"motion", "calibration", "limits", "relative", "hardware", "ui"}
    cli.set_config({"ui": {"theme": "light"}, "limits": {"leash_enabled": True}})
    assert cli.get_config()["ui"]["theme"] == "light"
    assert _wait(cli, lambda s: s.leash)


def test_moves_over_wire(service_and_client):
    _brain, cli = service_and_client
    assert cli.move_to_step("X", 2000) == 2000
    assert _wait(cli, lambda s: s.position_steps[0] == 2000 and not s.moving[0])
    assert cli.move_to_um("Y", 50.0) == 1000
    assert _wait(cli, lambda s: s.target_um[1] == 50.0 and not s.moving[1]
                 and abs(s.position_um[1] - 50.0) < 1e-9)
    cli.move_relative_um("Y", -5.0)
    assert _wait(cli, lambda s: s.position_steps[1] == 900 and not s.moving[1])


def test_amplitude_and_step_size_over_wire(service_and_client):
    _brain, cli = service_and_client
    assert cli.set_amplitude("X", 30, -1) == 30
    assert _wait(cli, lambda s: s.amplitude_bwd[0] == 30 and not s.cal_valid[0])
    assert cli.set_calibration("X", 0.08, -1) == 0.08
    assert _wait(cli, lambda s: s.cal_valid[0] and s.um_per_step_bwd[0] == 0.08)
    with pytest.raises(RuntimeError):     # {"ok": false} -> client raises
        cli.set_calibration("X", 0.0)
    st = cli.set_step_size(True)
    assert st["large"] is True
    assert _wait(cli, lambda s: s.step_large)


def test_jog_leash_datum_over_wire(service_and_client):
    _brain, cli = service_and_client
    assert cli.set_leash(enabled=True, leash_steps=150)["leash_steps"] == 150
    assert cli.move_to_step("X", 99999) == 150
    assert _wait(cli, lambda s: s.position_steps[0] == 150 and not s.moving[0])
    assert cli.jog("Y", -1) == -1
    assert _wait(cli, lambda s: s.jogging[1])
    cli.stop("Y")
    assert _wait(cli, lambda s: not s.jogging[1])
    cli.zero_counter("X")
    assert _wait(cli, lambda s: s.position_steps[0] == 0)
    # the describe actions are fired by their id
    assert cli._rpc(cmd="datum_y")["ok"]


def test_unknown_and_bad_commands(service_and_client):
    _brain, cli = service_and_client
    with pytest.raises(RuntimeError, match="unknown command"):
        cli._rpc(cmd="frobnicate")
    with pytest.raises(RuntimeError, match="bad axis"):
        cli.move_steps("Z", 10)


def test_position_list_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.move_to_step("Y", 200)
    assert _wait(cli, lambda s: s.position_steps[1] == 200 and not s.moving[1])
    cli.store_position(0, "wire")
    p = cli.get_positions()[0]
    assert p["used"] and p["name"] == "wire" and p["y"] == 200


def test_stream_verbs_over_wire(service_and_client):
    _brain, cli = service_and_client
    assert cli.stream_start(rate_hz=50) >= 1
    time.sleep(0.3)
    first = cli.stream_read()
    time.sleep(0.2)
    rest = cli.stream_stop()
    assert len(first["t"]) >= 8 and len(rest["t"]) >= 4
    assert first["t"][-1] < rest["t"][0]
    assert first["delay_s"] == {"x": 0.0, "y": 0.0}
    assert cli.stream_read()["t"] == []


def test_shutdown_verb_stops_the_service():
    brain, _ = build_sim_system(Config())
    svc = AgilisService(brain, host="127.0.0.1", cmd_port=CMD + 2, pub_port=PUB + 2)
    svc.start()
    cli = AgilisClient(host="127.0.0.1", cmd_port=CMD + 2, pub_port=PUB + 2, timeout_ms=3000)
    try:
        assert cli._rpc(cmd="shutdown")["stopping"] is True
        assert svc._stop.is_set()
    finally:
        cli.close()
        svc.stop()
    assert brain.status().connected is False
