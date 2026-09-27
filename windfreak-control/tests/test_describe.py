"""Tests for the `describe` manifest -- this service's self-description.

Properties that matter and are easy to get wrong:

  * every bound is read LIVE from cfg, never copied into a literal
  * `revision` changes when the STRUCTURE or the BOUNDS change (here also when
    the reference switches the external frequency between control and
    indicator), and not when a measured value changes
  * each settle rule names status keys that really exist, and waits for the
    echo AND the per-channel settled flag
"""

import time

import pytest

pytest.importorskip("zmq")

from windfreak.config import Config
from windfreak.sim_system import build_sim_system
from windfreak.net.describe import build_manifest, read_path
from windfreak.net.service import WindfreakService
from windfreak.net.client import WindfreakClient


@pytest.fixture
def brain():
    cfg = Config()
    synth, _ = build_sim_system(cfg)
    synth.start()
    yield cfg, synth
    synth.shutdown()


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields(brain):
    cfg, synth = brain
    m = build_manifest(synth)
    assert m["module"] == "windfreak"
    assert isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        assert p["type"] in ("float", "int", "bool", "enum", "string", "action"), p["id"]
        if p["kind"] == "control":
            assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
            assert "settle" in p, p["id"]
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]
        if p["kind"] == "action":
            assert "wait" in p, p["id"]


def test_both_channels_are_described_flat(brain):
    cfg, synth = brain
    by = _by_id(build_manifest(synth))
    for ch in "ab":
        for knob in ("rf_on", "frequency", "power", "phase"):
            p = by[f"{ch}_{knob}"]
            assert p["kind"] == "control"
            assert p["set"]["extra"] == {"channel": ch}
        for ind in ("locked", "leveled", "frequency_actual"):
            assert by[f"{ch}_{ind}"]["kind"] == "indicator"


def test_every_read_path_and_settle_key_exists_in_status(brain):
    cfg, synth = brain
    time.sleep(0.3)
    st = synth.status()
    for p in build_manifest(synth)["parameters"]:
        if p.get("read_path"):
            assert p["read_path"][0] in st, p["id"]
        # an action's wait flag must exist too (all_rf_off waits on rf_all_off)
        ready = (p.get("wait") or {}).get("ready") or {}
        if "key" in ready:
            assert ready["key"] in st, (p["id"], ready["key"])
        sb = p.get("settle") or {}
        for key in ("key", "setpoint_key", "flag_key"):
            if key in sb:
                assert sb[key] in st, (p["id"], sb[key])


def test_channel_controls_settle_on_echo_then_settled_flag(brain):
    cfg, synth = brain
    s = _by_id(build_manifest(synth))["b_frequency"]["settle"]
    assert s == {"policy": "adopt_then_flag", "setpoint_key": "b_frequency_Hz",
                 "flag_key": "b_settled"}


def test_limits_come_from_the_config(brain):
    cfg, synth = brain
    by = _by_id(build_manifest(synth))
    assert (by["a_power"]["min"], by["a_power"]["max"]) == (cfg.limits.power_min_dBm,
                                                            cfg.limits.power_max_dBm)
    f = by["b_frequency"]
    assert f["unit"] == "MHz" and f["scale"] == 1e6
    assert f["max"] == pytest.approx(cfg.limits.freq_max_Hz / 1e6)
    assert f["read_path"] == ["b_frequency_Hz"]


def test_revision_tracks_bounds_but_not_values(brain):
    cfg, synth = brain
    rev0 = build_manifest(synth)["revision"]
    synth.set_power("a", -3.0)
    time.sleep(0.1)
    assert build_manifest(synth)["revision"] == rev0, "a value moved the revision"
    cfg.limits.power_max_dBm = 10.0
    assert build_manifest(synth)["revision"] != rev0, "revision ignored a limit change"
    cfg.limits.power_max_dBm = 20.0
    assert build_manifest(synth)["revision"] == rev0


def test_reference_changes_the_manifest_shape(brain):
    cfg, synth = brain
    m0 = build_manifest(synth)
    assert _by_id(m0)["ext_ref"]["kind"] == "indicator"
    synth.set_reference("external", 10.0)
    m1 = build_manifest(synth)
    ext = _by_id(m1)["ext_ref"]
    assert ext["kind"] == "control"
    assert (ext["min"], ext["max"]) == (cfg.limits.ext_ref_min_MHz, cfg.limits.ext_ref_max_MHz)
    assert m1["revision"] != m0["revision"]


def test_read_path_survives_a_missing_branch(brain):
    cfg, synth = brain
    ind = [p for p in build_manifest(synth)["parameters"] if p["kind"] == "indicator"][0]
    assert read_path({}, ind["read_path"]) is None


def test_describe_over_the_wire_and_both_status_paths_agree(brain):
    """A field in only one status path is a field that vanishes intermittently."""
    cfg, synth = brain
    svc = WindfreakService(synth, host="127.0.0.1", cmd_port=17022, pub_port=17023)
    svc.start()
    client = None
    try:
        client = WindfreakClient(host="127.0.0.1", cmd_port=17022, pub_port=17023)
        client.start()
        m = client.describe()
        assert m["module"] == "windfreak"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
        # switching the reference over the wire is seen at once
        client.set_reference("external", 10.0)
        m2 = client.describe()
        assert client._cmd({"cmd": "status"})["status"]["describe_rev"] == m2["revision"]
        assert m2["revision"] != m["revision"]
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()


def test_all_rf_off_waits_on_the_instrument_not_on_the_reply(brain):
    """The reply only means accepted: the action must wait for a status flag."""
    cfg, synth = brain
    a = _by_id(build_manifest(synth))["all_rf_off"]
    assert a["wait"]["ready"] == {"policy": "flag_only", "key": "rf_all_off"}


def test_adopted_external_reference_shapes_the_manifest():
    """Read-only start: a SynthHD found on its external reference must be
    DESCRIBED that way (ext_ref a control), although the config default is
    internal -- and the revision must differ from the internal case."""
    from windfreak.backends.sim import SIM_BOOT_STATE
    boot = dict(SIM_BOOT_STATE, reference="external", ext_MHz=10.0)
    synth, _ = build_sim_system(Config(), external_ref_MHz=10.0, boot=boot)
    synth.start()
    try:
        m = build_manifest(synth)
        assert _by_id(m)["ext_ref"]["kind"] == "control"
    finally:
        synth.shutdown()
    synth2, _ = build_sim_system(Config())            # boots on internal 10 MHz
    synth2.start()
    try:
        m2 = build_manifest(synth2)
        assert _by_id(m2)["ext_ref"]["kind"] == "indicator"
        assert m2["revision"] != m["revision"]
    finally:
        synth2.shutdown()
