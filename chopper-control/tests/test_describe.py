"""Tests for the `describe` manifest -- this service's self-description.

What matters most and is easy to get wrong:
  * every bound is read LIVE from the brain (the blade's ring and the safety
    envelope), never copied into a literal;
  * `revision` changes when the STRUCTURE or the BOUNDS change (blade, ref
    mode, external reference), and not when a measured value changes;
  * the settle rule a scan waits on for the frequency is adopt-then-flag on
    `locked`, and the `start` action has a wait block keyed on its own reply.
"""

import pytest

pytest.importorskip("zmq")

from chopper.config import Config
from chopper.sim_system import build_sim_system
from chopper.net.describe import build_manifest, read_path
from chopper.net.protocol import status_to_dict
from chopper.net.service import ChopperService
from chopper.net.client import ChopperClient


def _brain(**sim):
    cfg = Config()
    for k, v in sim.items():
        setattr(cfg.sim, k, v)
    ch, _ = build_sim_system(cfg)
    ch.start(poll=False)
    return cfg, ch


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields():
    cfg, ch = _brain()
    try:
        m = build_manifest(ch)
        assert m["module"] == "chopper"
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
            if p["type"] == "enum":
                assert p["options"], p["id"]
    finally:
        ch.shutdown()


def test_every_read_path_resolves_against_a_real_status():
    cfg, ch = _brain()
    try:
        st = status_to_dict(ch.status())
        for p in build_manifest(ch)["parameters"]:
            if p.get("read_path"):
                assert p["read_path"][0] in st, p["id"]
        assert read_path({}, ["locked"]) is None          # must not raise
    finally:
        ch.shutdown()


def test_frequency_limits_follow_the_blade_ring_and_the_envelope():
    cfg, ch = _brain()
    try:
        f = _by_id(build_manifest(ch))["frequency"]
        assert (f["min"], f["max"]) == (20.0, 1000.0)       # 10/100 blade, inner ring
        assert f["unit"] == "Hz"
        assert f["settle"] == {"policy": "adopt_then_flag",
                               "setpoint_key": "setpoint_frequency_Hz",
                               "flag_key": "locked"}
        assert f["timeout_s"] == cfg.settle.timeout_s
        cfg.limits.freq_max_Hz = 500.0
        assert _by_id(build_manifest(ch))["frequency"]["max"] == 500.0
    finally:
        ch.shutdown()


def test_revision_tracks_blade_but_not_values():
    cfg, ch = _brain()
    try:
        rev0 = build_manifest(ch)["revision"]
        ch.set_frequency(333.0)                            # a value, not a bound
        assert build_manifest(ch)["revision"] == rev0
        ch.set_enable(False)
        ch.set_blade("MC1F60")
        m = build_manifest(ch)
        assert m["revision"] != rev0
        p = _by_id(m)
        assert (p["frequency"]["min"], p["frequency"]["max"]) == (120.0, 6000.0)
        assert p["ref_mode"]["options"] == ["internal", "external"]
        assert p["output_mode"]["options"] == ["target", "actual"]
        ch.set_blade("MC1F10HP")
        assert build_manifest(ch)["revision"] == rev0, \
            "revision is a counter, not derived from the manifest"
    finally:
        ch.shutdown()


def test_external_reference_turns_frequency_into_an_indicator():
    cfg, ch = _brain(external_input_Hz=100.0)
    try:
        ch.set_enable(False)
        ch.set_ref_mode("ext-inner")
        f = _by_id(build_manifest(ch))["frequency"]
        assert f["kind"] == "indicator" and "set" not in f
        assert f["read_path"] == ["target_frequency_Hz"]
    finally:
        ch.shutdown()


def test_start_action_waits_on_its_own_lock_generation():
    cfg, ch = _brain()
    try:
        p = _by_id(build_manifest(ch))
        w = p["start"]["wait"]
        assert w["target_key"] == "lock_gen"
        assert w["ready"] == {"policy": "adopt_then_flag", "setpoint_key": "lock_gen",
                              "flag_key": "locked"}
        assert p["stop"]["wait"]["ready"]["policy"] == "immediate"
    finally:
        ch.shutdown()


def test_describe_over_the_wire_and_both_status_paths_agree():
    """A field in only one status path is a field that vanishes intermittently."""
    cfg = Config()
    ch, _ = build_sim_system(cfg)
    svc = ChopperService(ch, host="127.0.0.1", cmd_port=17324, pub_port=17325)
    svc.start()
    client = None
    try:
        client = ChopperClient(host="127.0.0.1", cmd_port=17324, pub_port=17325)
        client.start()
        m = client.describe()
        assert m["module"] == "chopper"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
        assert "lock_gen" in direct and "locked" in direct
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()
