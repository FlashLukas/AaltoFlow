"""End-to-end over ZeroMQ: the service on the simulated bench and a client on
loopback. This module's own test ports (17640..17649), so it never collides
with a running service or a sibling module's tests."""

import time

import numpy as np
import pytest

from scope.config import Config
from scope.sim_system import build_sim_system
from scope.net.service import ScopeService
from scope.net.client import ScopeClient

CMD_PORT = 17640
PUB_PORT = 17641


@pytest.fixture
def pair():
    cfg = Config()
    scope, sim = build_sim_system(cfg, seed=7)
    svc = ScopeService(scope, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                       status_hz=20.0)
    svc.start()
    cli = ScopeClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=3000)
    cli.start()
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    svc._cmd_t.join(timeout=2.0)
    svc._pub_t.join(timeout=2.0)


def wait_for(cli, pred, timeout=8.0):
    t_end = time.monotonic() + timeout
    st = {}
    while time.monotonic() < t_end:
        st = cli._cmd({"cmd": "status"}).get("status", {})
        if st and pred(st):
            return st
        time.sleep(0.02)
    raise AssertionError(f"not reached; last {st}")


def test_info(pair):
    _, cli, _ = pair
    assert cli.channels == ("ch1", "ch2") and cli.simulated is True
    assert cli.caps["generator_channels"] == 0


def test_acquire_blocking_returns_traces_and_numbers(pair):
    _, cli, _ = pair
    cli.set_averages(2)
    cli.set_physical("ch1", scale=10.0, unit="A")
    wait_for(cli, lambda s: s["ch1_unit"] == "A" and s["averages"] == 2)
    tr = cli.acquire_blocking(timeout_s=10)
    assert isinstance(tr["ch1"], np.ndarray) and tr["ch1"].size == 1000
    assert tr["ch1_values"]["amplitude"] == pytest.approx(10.0, rel=0.03)
    assert tr["phase_21_deg"] == pytest.approx(60.0, abs=2.0)
    assert np.allclose(cli.get_time(), tr["time_s"])
    live = cli.get_trace("live")
    assert live["ch2"].size == 1000


def test_scope_setting_over_the_wire(pair):
    _, cli, sim = pair
    assert cli.set_vdiv("ch2", 0.2)["ok"]
    wait_for(cli, lambda s: s["ch2_vdiv_V_set"] == 0.2 and s["settings_settled"]
             and s["ch2_vdiv_V"] == 0.2)
    assert sim.settings["channels"]["ch2"]["vdiv_V"] == 0.2


def test_bad_requests_answer_ok_false(pair):
    _, cli, _ = pair
    assert cli._cmd({"cmd": "set_vdiv", "channel": "ch3", "vdiv_V": 1})["ok"] is False
    assert cli._cmd({"cmd": "set_coupling", "channel": "ch1", "coupling": "x"})["ok"] is False
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    r = cli._cmd({"cmd": "get_trace", "which": "sample"})
    assert r["ok"] is False and "latched" in r["error"]
    assert cli._cmd({"cmd": "status"})["ok"] is True       # still alive


def test_stopped_scope_refuses_acquire(pair):
    _, cli, _ = pair
    cli.set_trigger_mode("stop")
    wait_for(cli, lambda s: s["trigger_mode"] == "stop" and s["settings_settled"])
    with pytest.raises(ValueError, match="stopped"):
        cli.acquire()


def test_set_config_text_bool_and_shutdown(pair):
    svc, cli, _ = pair
    r = cli._cmd({"cmd": "set_config", "config": {"acquisition": {"keep_raw": "false"}}})
    assert r["ok"]
    wait_for(cli, lambda s: s["keep_raw"] is False)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": "false"})   # text: parsed, gotcha #3
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is False


def test_restart_keeps_the_scope_untouched(pair):
    """shutdown{keep_outputs: true} (the launcher's Restart): replies
    kept_outputs, and nothing is written to the scope on the way out."""
    svc, cli, sim = pair
    before = list(sim.writes)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is True
    svc.stop()
    assert sim.writes == before


def test_quantity_survives_a_restart_and_shapes_describe(tmp_path):
    """Lukas 2026-10-07: the QUANTITY settings (a probe's calibration) "need to
    be saved". Every change goes to scope.ini at once (atomically); a new
    service started from that file has them -- and describe's detector units
    follow at once (describe_rev moves), so a scan records A, not V."""
    from scope.config import Config as C
    ini = tmp_path / "scope.ini"
    scope, _ = build_sim_system(C(), seed=1)
    scope.persist_path = str(ini)
    svc = ScopeService(scope, host="127.0.0.1", cmd_port=17642, pub_port=17643, status_hz=20)
    svc.start()
    cli = ScopeClient(host="127.0.0.1", cmd_port=17642, pub_port=17643, timeout_ms=3000)
    try:
        cli.start()
        rev0 = cli._cmd({"cmd": "status"})["status"]["describe_rev"]
        cli.set_physical("ch1", scale=10.0, unit="A", label="Current")
        cli.set_averages(64)
        cli.set_filter(lowpass_Hz=1500.0)
        st = wait_for(cli, lambda s: s["ch1_unit"] == "A" and s["describe_rev"] != rev0)
        by = {p["id"]: p for p in cli.describe()["parameters"]}
        assert by["ch1"]["unit"] == "A" and by["ch1_mean"]["unit"] == "A"
    finally:
        cli.shutdown()
        svc.stop()
        svc._cmd_t.join(timeout=2.0)
        svc._pub_t.join(timeout=2.0)
    assert ini.is_file() and not (tmp_path / "scope.ini.tmp").exists()
    back = C.load(str(ini))                      # what the next start reads
    assert back.channel_1.phys_scale == 10.0 and back.channel_1.phys_unit == "A"
    assert back.channel_1.phys_label == "Current"
    assert back.acquisition.averages == 64 and back.filter.lowpass_Hz == 1500.0


def test_nothing_is_written_without_a_persist_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scope, _ = build_sim_system(Config())
    scope.set_averages(3)
    assert list(tmp_path.iterdir()) == []
