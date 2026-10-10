"""The Analog Discovery's generator and supplies over ZeroMQ: the gen_* verbs,
set_supply / supplies_off, the remote stand-in the GUI uses (ScopeClient.gen)
and the safety verbs a VIEWER may send. Test ports 17644/17645 (this
module's block is 17640..17649)."""

import time

import pytest

from scope.config import Config
from scope.sim_system import build_sim_system
from scope.net.service import ScopeService
from scope.net.client import ScopeClient
from scope.control import ControlRefused

CMD, PUB = 17644, 17645


@pytest.fixture
def pair():
    cfg = Config()
    cfg.sim.model = "ad"
    scope, sim = build_sim_system(cfg, seed=2)
    svc = ScopeService(scope, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20.0)
    svc.start()
    cli = ScopeClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
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


def test_generator_over_the_wire(pair):
    svc, cli, sim = pair
    assert cli.caps["generator_channels"] == 2 and cli.gen is not None
    assert cli.gen.channels == ("w1", "w2")
    assert cli.gen.caps["load_settable"] is False
    assert cli.gen.set_frequency("w1", 2500.0)["ok"]
    assert cli.gen.set_output("w1", True)["ok"]
    st = wait_for(cli, lambda s: s["w1_output"] and s["w1_settled"])
    assert st["w1_frequency_Hz"] == 2500.0
    # the generator's own keys again (from the status stream: wait for a frame)
    t_end = time.monotonic() + 3
    g = cli.gen.status()
    while g.get("w1_frequency_Hz") != 2500.0 and time.monotonic() < t_end:
        time.sleep(0.05)
        g = cli.gen.status()
    assert g["w1_frequency_Hz"] == 2500.0 and "follow" in g and "connected" in g
    assert cli.gen.envelope("w1")["peak_max_V"] == pytest.approx(5.0)
    r = cli._cmd({"cmd": "gen_set_load", "channel": "w1", "load": "50"})
    assert r["ok"] is False and "no load" in r["error"]
    assert cli.gen.set_follow(True, 30.0, True)["ok"]
    st = wait_for(cli, lambda s: s["gen_follow"] and s["w2_frequency_Hz"] == 2500.0)
    op = cli.gen.outputs_off()["op_id"]
    wait_for(cli, lambda s: s["gen_op_id"] >= op and s["gen_all_off"])
    assert sim.gen.ch[0]["output"] is False


def test_supplies_over_the_wire(pair):
    svc, cli, sim = pair
    assert cli.has_supplies() and cli.supply_limits("vminus") == (-5.0, -0.5)
    assert cli.set_supply("vplus", on=True, volts=1.8)["ok"]
    st = wait_for(cli, lambda s: s["supply_vplus_on"])
    assert st["supply_vplus_V"] == pytest.approx(1.8)
    assert "USB Monitor Voltage V" in st["monitors"]
    assert cli.supplies_off()["ok"]
    wait_for(cli, lambda s: not s["supply_vplus_on"])


def test_a_viewer_may_switch_things_off_but_not_on(pair):
    svc, cli, sim = pair
    cli.set_supply("vplus", on=True, volts=1.0)
    cli.gen.set_output("w1", True)
    wait_for(cli, lambda s: s["supply_vplus_on"] and s["w1_output"])
    other = ScopeClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
                        kind="gui", name="viewer")
    # two different PCs (the lease tells clients apart by who they are)
    cli.identity["host"] = "user@pc-a"
    other.identity["host"] = "user@pc-b"
    try:
        assert cli.take_control()
        other.start()
        for cmd in ({"cmd": "set_supply", "supply": "vminus", "on": True},
                    {"cmd": "gen_set_output", "channel": "w2", "on": True}):
            with pytest.raises(ControlRefused):                 # needs control
                other._cmd(cmd)
        assert other._cmd({"cmd": "supplies_off"})["ok"]          # safety: allowed
        assert other._cmd({"cmd": "gen_outputs_off"})["ok"]
        wait_for(cli, lambda s: not s["supply_vplus_on"] and s["gen_all_off"])
    finally:
        other.shutdown()
