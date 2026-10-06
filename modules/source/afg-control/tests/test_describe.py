"""Tests for the `describe` manifest -- this service's self-description.

Properties that matter and are easy to get wrong:

  * every bound is read LIVE (the brain's envelope: lab limits narrowed by the
    instrument's range for the current waveform and load), never a literal
  * `revision` changes when the STRUCTURE or the BOUNDS change -- a new
    waveform, a new load, the coupling switched -- and not when a value moves
  * each settle rule names status keys that really exist, and waits for the
    echo AND the per-channel settled flag
  * every value status reports fits its declared type (scan-core stores by it)
"""

import time

import pytest

pytest.importorskip("zmq")

from afg.config import Config
from afg.sim_system import build_sim_system
from afg.net.describe import build_manifest, read_path
from afg.net.service import AfgService
from afg.net.client import AfgClient


@pytest.fixture
def brain():
    cfg = Config()
    gen, _ = build_sim_system(cfg)
    gen.start()
    yield cfg, gen
    gen.shutdown()


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def _wait(gen, pred, timeout=2.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if pred(gen.status()):
            return
        time.sleep(0.01)
    raise AssertionError("not reached")


def test_manifest_shape_and_required_fields(brain):
    cfg, gen = brain
    m = build_manifest(gen)
    assert m["module"] == "afg"
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
    cfg, gen = brain
    by = _by_id(build_manifest(gen))
    for ch in ("ch1", "ch2"):
        for knob in ("output", "waveform", "frequency", "amplitude", "offset", "phase", "load"):
            p = by[f"{ch}_{knob}"]
            assert p["kind"] == "control", p["id"]
            assert p["set"]["extra"] == {"channel": ch}
        for ind in ("peak", "settled", "mismatch", "mode", "frequency_actual"):
            assert by[f"{ch}_{ind}"]["kind"] == "indicator"


def test_every_read_path_and_settle_key_exists_in_status(brain):
    cfg, gen = brain
    gen.set_follow(True)                     # the phase offset control too
    gen.set_waveform("ch1", "pulse")         # and the duty control
    _wait(gen, lambda s: s["ch1_waveform"] == "pulse")
    st = gen.status()
    for p in build_manifest(gen)["parameters"]:
        if p.get("read_path"):
            assert p["read_path"][0] in st, p["id"]
        w = p.get("wait") or {}
        for blk in (w.get("ready") or {}, p.get("settle") or {}):
            for key in ("key", "setpoint_key", "flag_key"):
                if key in blk:
                    assert blk[key] in st, (p["id"], blk[key])
        if "target_key" in w:
            assert w["target_key"] in st, p["id"]


def test_channel_controls_settle_on_echo_then_settled_flag(brain):
    cfg, gen = brain
    s = _by_id(build_manifest(gen))["ch2_amplitude"]["settle"]
    assert s == {"policy": "adopt_then_flag", "setpoint_key": "ch2_amplitude_Vpp",
                 "flag_key": "ch2_settled"}


def test_limits_are_the_live_envelope(brain):
    cfg, gen = brain
    by = _by_id(build_manifest(gen))
    assert by["ch1_frequency"]["max"] == 60e6                  # sine, AFG1062
    assert by["ch1_amplitude"]["max"] == 10.0
    assert by["ch1_offset"]["max"] == 5.0          # the AFG at 50 ohm (lab limit = full range)
    cfg.limits_1.peak_max_V = 2.0
    cfg.limits_1.freq_max_Hz = 1000.0
    by = _by_id(build_manifest(gen))
    assert by["ch1_offset"]["max"] == 2.0 and by["ch1_frequency"]["max"] == 1000.0


def test_waveform_changes_shape_and_revision(brain):
    cfg, gen = brain
    rev0 = build_manifest(gen)["revision"]
    gen.set_amplitude("ch1", 1.0)             # a VALUE: no new revision
    _wait(gen, lambda s: s["ch1_amplitude_Vpp"] == 1.0)
    assert build_manifest(gen)["revision"] == rev0
    gen.set_waveform("ch1", "ramp")
    _wait(gen, lambda s: s["ch1_waveform"] == "ramp")
    by = _by_id(build_manifest(gen))
    assert by["ch1_frequency"]["max"] == 1e6 and "ch1_symmetry" in by
    assert "ch1_duty" not in by
    gen.set_waveform("ch1", "dc")
    _wait(gen, lambda s: s["ch1_waveform"] == "dc")
    m = build_manifest(gen)
    by = _by_id(m)
    assert by["ch1_frequency"]["kind"] == "indicator"
    assert by["ch1_amplitude"]["kind"] == "indicator"
    assert by["ch1_offset"]["label"].endswith("DC level")
    assert m["revision"] != rev0


def test_follow_turns_ch2_frequency_into_an_indicator(brain):
    cfg, gen = brain
    by = _by_id(build_manifest(gen))
    assert by["phase_offset"]["kind"] == "indicator"
    gen.set_follow(True, 45.0)
    by = _by_id(build_manifest(gen))
    assert by["ch2_frequency"]["kind"] == "indicator"
    assert by["ch2_phase"]["kind"] == "indicator"
    assert by["phase_offset"]["kind"] == "control"
    assert by["ch1_frequency"]["kind"] == "control"


def test_actions_wait_on_a_numbered_operation(brain):
    """The reply only means accepted: the actions wait for THEIR number."""
    cfg, gen = brain
    by = _by_id(build_manifest(gen))
    for aid in ("outputs_off", "align_phase"):
        w = by[aid]["wait"]
        assert w["target_key"] == "op_id"
        assert w["ready"] == {"policy": "adopt_then_flag", "setpoint_key": "op_id",
                              "flag_key": "op_ok"}


def test_read_path_survives_a_missing_branch(brain):
    cfg, gen = brain
    ind = [p for p in build_manifest(gen)["parameters"] if p["kind"] == "indicator"][0]
    assert read_path({}, ind["read_path"]) is None


def test_describe_over_the_wire_and_both_status_paths_agree(brain):
    """A field in only one status path is a field that vanishes intermittently."""
    cfg, gen = brain
    svc = AfgService(gen, host="127.0.0.1", cmd_port=17622, pub_port=17623)
    svc.start()
    client = None
    try:
        client = AfgClient(host="127.0.0.1", cmd_port=17622, pub_port=17623)
        client.start()
        m = client.describe()
        assert m["module"] == "afg"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
        # a new waveform over the wire is seen at once
        client.set_waveform("ch2", "pulse")
        _wait(gen, lambda s: s["ch2_waveform"] == "pulse")
        m2 = client.describe()
        assert client._cmd({"cmd": "status"})["status"]["describe_rev"] == m2["revision"]
        assert m2["revision"] != m["revision"]
        # the operation verbs answer with their number
        r = client.outputs_off()
        assert r["ok"] and isinstance(r["op_id"], int)
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()


# ---- declared types: how scan-core STORES each value (developer notes 4b) ---

def _fits(d, v):
    """Does status value `v` fit descriptor `d` the way scan-core stores it?"""
    if v is None:
        return True
    t = d["type"]
    if t == "bool":
        return isinstance(v, bool)
    if t == "enum":
        return v in d["options"]
    if t == "string":
        return isinstance(v, str)
    if t == "float":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return True


@pytest.mark.parametrize("waveform", ["sine", "square", "pulse", "ramp", "noise", "dc"])
def test_every_status_value_fits_its_type(brain, waveform):
    from afg.net.protocol import status_to_dict
    cfg, gen = brain
    gen.set_waveform("ch1", waveform)
    gen.set_load("ch2", "high-Z")
    _wait(gen, lambda s: s["ch1_waveform"] == waveform and s["ch2_load"] == "high-Z")
    st = status_to_dict(gen.status())
    for d in build_manifest(gen)["parameters"]:
        if d.get("read_path") and d["kind"] in ("indicator", "control"):
            v = read_path(st, d["read_path"])
            assert _fits(d, v), f"{d['id']}: {v!r} does not fit {d}"
