"""Tests for the `describe` manifest -- this service's self-description.

Properties that matter and are easy to get wrong:

  * every bound is read LIVE from cfg AND the unit's own range, never copied
    into a literal
  * `revision` changes when the STRUCTURE or the BOUNDS change (config edit,
    connecting to a unit with a narrower range, a unit without phase), and not
    when a measured value changes
  * every settle rule names a key the status actually publishes
"""

import pytest

pytest.importorskip("zmq")

from dssg.config import Config
from dssg.sim_system import build_sim_system
from dssg.net.describe import build_manifest, read_path
from dssg.net.protocol import status_to_dict
from dssg.net.service import DssgService
from dssg.net.client import DssgClient


def _brain(cfg=None):
    cfg = cfg or Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    return cfg, brain


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields():
    cfg, brain = _brain()
    try:
        m = build_manifest(brain)
        assert m["module"] == "dssg"
        assert isinstance(m["revision"], int)
        ids = [p["id"] for p in m["parameters"]]
        assert len(ids) == len(set(ids)), "duplicate parameter ids"
        assert {"rf_on", "frequency", "power", "phase", "reference",
                "usb_volts", "ext_ref_detected"} <= set(ids)
        status = status_to_dict(brain.status())
        for p in m["parameters"]:
            assert p["kind"] in ("control", "indicator", "action"), p["id"]
            if p["kind"] == "control":
                assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
                # a settle key the status does not publish would hang a scan
                assert p["settle"]["key"] in status, p["id"]
            if p["kind"] == "action":
                continue        # a button (the safety verb rf_off): nothing to read back
            assert p["read_path"], p["id"]
            assert read_path(status, p["read_path"]) is not None, p["id"]
    finally:
        brain.shutdown()


def test_limits_are_the_intersection_of_cfg_and_the_unit():
    cfg, brain = _brain()
    try:
        by = _by_id(build_manifest(brain))
        assert by["power"]["min"] == cfg.sim.power_min_dBm          # unit is narrower
        assert by["power"]["max"] == cfg.limits.power_max_dBm       # cfg is narrower
        assert by["frequency"]["max"] == pytest.approx(cfg.sim.freq_max_Hz / 1e6)
        assert by["frequency"]["unit"] == "MHz" and by["frequency"]["scale"] == 1e6
        assert by["reference"]["options"] == ["internal", "external", "auto"]
    finally:
        brain.shutdown()


def test_power_echo_tolerance_covers_half_an_attenuator_step():
    cfg, brain = _brain()
    try:
        tol = _by_id(build_manifest(brain))["power"]["settle"]["tol"]
        assert tol >= cfg.hardware.power_step_dB / 2
        assert tol < cfg.hardware.power_step_dB
    finally:
        brain.shutdown()


def test_revision_tracks_bounds_but_not_values():
    cfg, brain = _brain()
    try:
        rev0 = build_manifest(brain)["revision"]
        brain.set_frequency(3e9)
        brain.set_rf(True)
        assert build_manifest(brain)["revision"] == rev0, "a value changed the revision"
        cfg.limits.power_max_dBm = 0.0
        assert build_manifest(brain)["revision"] != rev0, "revision ignored a limit change"
        cfg.limits.power_max_dBm = Config().limits.power_max_dBm
        assert build_manifest(brain)["revision"] == rev0
    finally:
        brain.shutdown()


def test_connecting_changes_the_revision():
    """Before start the unit's range is unknown (cfg only); after, it is known."""
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    before = build_manifest(brain)
    brain.start()
    try:
        after = build_manifest(brain)
        assert after["revision"] != before["revision"]
        assert _by_id(after)["frequency"]["max"] < _by_id(before)["frequency"]["max"]
    finally:
        brain.shutdown()


def test_unit_without_phase_drops_the_phase_control():
    cfg = Config()
    cfg.sim.has_phase = False
    _, brain = _brain(cfg)
    try:
        assert "phase" not in _by_id(build_manifest(brain))
    finally:
        brain.shutdown()


def test_describe_over_the_wire_and_both_status_paths_agree():
    """A field in only one status path is a field that vanishes intermittently."""
    cfg, brain = _brain()
    svc = DssgService(brain, host="127.0.0.1", cmd_port=17122, pub_port=17123)
    svc.start()
    client = None
    try:
        client = DssgClient(host="127.0.0.1", cmd_port=17122, pub_port=17123)
        client.start()
        m = client.describe()
        assert m["module"] == "dssg"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"], \
            "the status command reply and the manifest disagree"
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()


def test_echo_tolerances_come_from_config_and_cover_the_synth_grid():
    """A tolerance finer than what the box can report would hang a scan: the
    fractional-N grid is up to ~3 kHz, so half of it must fit in the tolerance."""
    cfg = Config()
    cfg.hardware.phase_echo_tol_deg = 0.25
    _, brain = _brain(cfg)
    try:
        by = _by_id(build_manifest(brain))
        assert by["frequency"]["settle"]["tol"] >= 1500.0
        assert by["phase"]["settle"]["tol"] == 0.25
    finally:
        brain.shutdown()


# ---- declared types: how scan-core STORES each detector (developer notes 4b) ---

def _fits(d, v):
    """Does status value `v` fit descriptor `d` the way scan-core stores it?
    (bool a bool, int a whole number inside an INDICATOR's min/max, enum one
    of its options; None = not measured always fits.)"""
    if v is None:
        return True
    t = d["type"]
    if t == "bool":
        return isinstance(v, bool)
    if t == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            return False
        if d["kind"] == "indicator":
            return d.get("min", v) <= v <= d.get("max", v)
        return True
    if t == "enum":
        return v in d["options"]
    if t == "string":
        return isinstance(v, str)
    if t == "float":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return True


def _check_types(brain):
    """Every indicator/control read from status fits its declared type."""
    from dssg.net.protocol import status_to_dict
    st = status_to_dict(brain.status())
    for d in build_manifest(brain)["parameters"]:
        if d.get("read_path") and d["kind"] in ("indicator", "control"):
            v = read_path(st, d["read_path"])
            assert _fits(d, v), f"{d['id']}: {v!r} does not fit {d}"
    return st


def test_reference_options_cover_every_value_status_reports():
    """The reference enum's options ARE config.REFERENCES; parse_reference
    maps ANY *REFMODE? reply onto one of them, and status shows each after it
    is set -- a value outside the options would be stored as "not measured"."""
    import time
    from dssg.backends.dsi_scpi import parse_reference
    from dssg.config import REFERENCES
    cfg, brain = _brain()
    try:
        d = _by_id(build_manifest(brain))["reference"]
        assert d["type"] == "enum" and d["options"] == list(REFERENCES)
        for reply in ("", "0", "1", "2", "AUTO", "EXT", "INT", "garbage"):
            assert parse_reference(reply) in REFERENCES
        for mode in REFERENCES:
            brain.set_reference(mode)
            deadline = time.monotonic() + 5.0
            while brain.status().reference != mode and time.monotonic() < deadline:
                time.sleep(0.05)
            assert _check_types(brain)["reference"] == mode
    finally:
        brain.shutdown()
