"""The Analog Discovery, simulated (cfg.sim.model = "ad"): scope + generator
W1/W2 looped back to CH1/CH2 (the lab bench, Lukas 2026-10-08) + V+/V-.

Each rule has a test that fails without it:
  * start adopts: generator outputs and supplies are found OFF and left
    alone -- nothing is written at start;
  * the loopback: what the generator brain makes is what the scope measures
    (amplitude, frequency, the phase W2 - W1 as CH2 - CH1);
  * supplies: clamped to the limits (warn), off at shutdown, kept on a
    restart (keep_outputs);
  * trigger sources come from the instrument (T1/T2 and W1/W2; no "line"),
    and the AD never "rolls";
  * describe: the generator and the supplies, namespaced, ids unique, every
    read path resolves.
"""

import time

import pytest

from scope.config import Config
from scope.sim_system import build_sim_system
from scope.net.describe import build_manifest, read_path
from scope.net.protocol import status_to_dict


@pytest.fixture
def ad():
    cfg = Config()
    cfg.sim.model = "ad"
    scope, sim = build_sim_system(cfg, seed=3)
    events = []
    scope._on_event = lambda level, msg: events.append((level, msg))
    scope.start()
    yield scope, sim, cfg, events
    scope.shutdown()


def wait(scope, pred, timeout=8.0):
    t_end = time.monotonic() + timeout
    st = scope.status()
    while time.monotonic() < t_end:
        st = scope.status()
        if pred(st):
            return st
        time.sleep(0.02)
    raise AssertionError(f"not reached; last {st}")


def test_start_adopts_and_writes_nothing(ad):
    scope, sim, cfg, events = ad
    st = wait(scope, lambda s: s.get("gen_connected"))
    assert st["generator_channels"] == 2
    assert st["w1_output"] is False and st["w2_output"] is False
    assert st["supply_vplus_on"] is False and st["supply_vminus_on"] is False
    assert sim.writes == [] and sim.gen.writes == []
    assert any("supplies found" in m and "left as they are" in m for _, m in events)


def test_the_loopback_is_measured(ad):
    scope, sim, cfg, events = ad
    g = scope.gen
    g.set_waveform("w1", "sine"); g.set_frequency("w1", 1000.0)
    g.set_amplitude("w1", 1.0); g.set_output("w1", True)
    g.set_frequency("w2", 1000.0); g.set_amplitude("w2", 0.5)
    g.set_phase("w2", 60.0); g.set_output("w2", True)
    wait(scope, lambda s: s["w1_settled"] and s["w2_settled"] and s["w2_output"])
    scope.set_averages(4)
    n = scope.acquire()
    st = wait(scope, lambda s: s["acq_id"] == n and not s["acquiring"], 15)
    smp = st["sample"]
    # from the rms (a pk-pk picks up the noise peaks): Vpp = 2 sqrt(2) rms
    assert smp["ch1"]["rms"] * 2 * 2 ** 0.5 == pytest.approx(1.0, rel=0.02)
    assert smp["ch2"]["rms"] * 2 * 2 ** 0.5 == pytest.approx(0.5, rel=0.02)
    assert smp["ch1"]["frequency"] == pytest.approx(1000.0, rel=1e-3)
    assert smp["phase_21_deg"] == pytest.approx(60.0, abs=0.5)
    # the sim's input zero (a few mV) shows in the mean, as on a real input
    assert smp["ch1"]["mean"] == pytest.approx(sim.ch_zero_V["ch1"], abs=2e-3)


def test_supplies_clamp_off_at_shutdown_and_kept_on_restart():
    for keep in (False, True):
        cfg = Config(); cfg.sim.model = "ad"
        cfg.supplies.vplus_max_V = 3.3
        scope, sim = build_sim_system(cfg, seed=1)
        events = []
        scope._on_event = lambda level, msg: events.append((level, msg))
        scope.start()
        scope.set_supply("vplus", on=True, volts=5.0)          # beyond the lab limit
        scope.set_supply("V-", on=True, volts=-2.0)
        st = scope.status()
        assert st["supply_vplus_V"] == pytest.approx(3.3) and st["supply_vplus_on"]
        assert st["supply_vminus_V"] == pytest.approx(-2.0)
        assert any(lvl == "warn" and "clamped to 3.3" in m for lvl, m in events)
        with pytest.raises(ValueError):
            scope.set_supply("v5", on=True)
        scope.shutdown(keep_outputs=keep)
        on = [k for k, s in sim.supplies.items() if s["on"]]
        assert on == ([] if not keep else ["vplus", "vminus"]), keep


def test_generator_outputs_off_at_shutdown_unless_kept():
    for keep in (False, True):
        cfg = Config(); cfg.sim.model = "ad"
        scope, sim = build_sim_system(cfg, seed=1)
        scope.start()
        scope.gen.set_output("w1", True)
        wait(scope, lambda s: s["w1_output"] and s["w1_settled"])
        scope.shutdown(keep_outputs=keep)
        assert sim.gen.ch[0]["output"] is keep


def test_trigger_sources_come_from_the_instrument(ad):
    scope, sim, cfg, events = ad
    assert scope.trigger_sources() == ["ch1", "ch2", "ext1", "ext2", "w1", "w2"]
    with pytest.raises(ValueError):
        scope.set_trigger_source("line")                       # a Siglent source
    scope.set_trigger_source("ext1")
    wait(scope, lambda s: s["trigger_source"] == "ext1" and s["settings_settled"])
    assert scope.trigger_options() == {"level": False, "slope": True}
    ids = {p["id"] for p in build_manifest(scope)["parameters"]}
    assert "trigger_slope" in ids and "trigger_level" not in ids
    scope.set_trigger_source("w1")
    wait(scope, lambda s: s["trigger_source"] == "w1" and s["settings_settled"])
    ids = {p["id"] for p in build_manifest(scope)["parameters"]}
    assert "trigger_slope" not in ids and "trigger_level" not in ids
    with pytest.raises(ValueError):
        scope.set_coupling("ch1", "ac")                         # DC only
    scope.set_tdiv(0.5)                                         # never rolls
    scope.set_trigger_mode("auto")
    assert scope.status()["rolling"] is False


def test_a_generator_trigger_triggers(ad):
    scope, sim, cfg, events = ad
    g = scope.gen
    g.set_frequency("w2", 200.0); g.set_output("w2", True)
    scope.set_trigger_source("w2"); scope.set_trigger_mode("normal")
    wait(scope, lambda s: s["trigger_mode"] == "normal" and s["settings_settled"]
         and s["w2_output"])
    r0 = scope.status()["records"]
    wait(scope, lambda s: s["records"] >= r0 + 2)


def test_describe_has_generator_and_supplies(ad):
    scope, sim, cfg, events = ad
    wait(scope, lambda s: s.get("gen_connected") and s["records"] > 2)
    m = build_manifest(scope)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids))
    for want in ("w1_frequency", "w2_phase", "gen_follow", "gen_outputs_off",
                 "supply_vplus_on", "supply_vminus_V", "supplies_off"):
        assert want in ids, want
    byid = {p["id"]: p for p in m["parameters"]}
    assert byid["w1_frequency"]["set"]["verb"] == "gen_set_frequency"
    assert byid["w1_frequency"]["settle"]["flag_key"] == "w1_settled"
    assert byid["supply_vplus_on"].get("danger") is True
    assert byid["supply_vplus_V"]["max"] == pytest.approx(5.0)
    st = status_to_dict(scope.status())
    missing = [p["id"] for p in m["parameters"]
               if p.get("read_path") and p["read_path"][0] != "sample"
               and p["type"] != "string" and read_path(st, p["read_path"]) is None]
    assert not missing, missing
