"""The describe manifest: complete, consistent with status, and honest about
what changes it."""

import json

import pytest

from fake_sa_api import FakeSaApi
from signalhound.backends.sa_api import SaApiAnalyzer
from signalhound.config import Config
from signalhound.net.describe import build_manifest, read_path
from signalhound.net.protocol import status_to_dict
from signalhound.sim_system import build_sim_system
from signalhound.spectrum import SpectrumAnalyzer


@pytest.fixture
def signalhound():
    cfg = Config()
    cfg.acquisition.continuous = False
    v, _ = build_sim_system(cfg, realtime=False, seed=1)
    v.start(run=False)
    yield v
    v.shutdown()


def _params(v):
    return {p["id"]: p for p in build_manifest(v)["parameters"]}


def test_manifest_is_json_and_ids_are_unique(signalhound):
    m = build_manifest(signalhound)
    json.dumps(m, allow_nan=False)                  # no NaN, no numpy types
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids))
    assert m["module"] == "signalhound" and "SA44B" in m["label"]


def test_every_read_path_resolves_in_status(signalhound):
    # an acquisition and a CW too, so the sample / TG paths hold values
    signalhound.tg_cw(True, 1e9, -20.0)
    signalhound.acquire()
    while signalhound.status().acquiring:
        signalhound.step()
    st = status_to_dict(signalhound.status())
    for p in build_manifest(signalhound)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None, p["id"]


def test_trace_is_a_real_array_detector(signalhound):
    tr = _params(signalhound)["trace"]
    assert tr["type"] == "array" and tr["dtype"] == "float" and tr["shape"] == ["freq"]
    dim = tr["dims"][0]
    assert dim["coord_verb"] == "get_frequencies" and dim["coord_key"] == "values_GHz"
    assert dim["length"] == signalhound.status().points
    assert tr["acquire"]["target_key"] == "acq_id"
    assert tr["read"] == {"verb": "get_trace", "key": "trace",
                          "args": {"which": "sample", "quantity": "trace"}}


def test_scalar_detectors_share_the_one_acquisition(signalhound):
    params = _params(signalhound)
    ids = ("trace", "peak_freq", "peak_level", "noise_floor", "overloaded")
    assert {params[k]["acquire"]["group"] for k in ids} == {"sweep"}
    assert "wait" not in params["acquire"] and "wait" not in params["abort"]


def test_the_tg_is_shown_but_not_driven_here(signalhound):
    """Lukas 2026-09-28: the TG's verbs are for the client modules (shsg,
    shsna), which offer the scan controls; this panel only SHOWS the TG."""
    params = _params(signalhound)
    tg = {k: p for k, p in params.items() if p.get("group") == "Tracking generator"}
    assert {"tg_mode", "tg_cw_on", "tg_cw_freq", "tg_cw_level", "tg_attached"} <= set(tg)
    assert all(p["kind"] == "indicator" for p in tg.values())
    for p in params.values():
        assert not str((p.get("set") or {}).get("verb", "")).startswith("tg_"), p["id"]
        assert not p["id"].startswith(("tg_cw_set", "tg_sweep")), p["id"]
    gone = {"transmission", "tx_center", "take_reference", "clear_reference", "tg_on",
            "tg_level", "tg_points", "reference_present"}
    assert not gone & set(params)


def test_action_verbs_exist(signalhound):
    from signalhound.net.service import SignalhoundService
    svc = SignalhoundService(signalhound)           # not started: only _dispatch is used
    for p in build_manifest(signalhound)["parameters"]:
        if p["kind"] == "action":
            reply = svc._dispatch({"cmd": p["id"]})
            assert reply["ok"], (p["id"], reply)


def test_every_setter_verb_exists_and_settles_on_a_status_key(signalhound):
    from signalhound.net.service import SignalhoundService
    from scan_like import lookup
    svc = SignalhoundService(signalhound)
    for p in build_manifest(signalhound)["parameters"]:
        spec = p.get("set")
        if not spec:
            continue
        reply = svc._dispatch({"cmd": spec["verb"], spec["arg"]: _probe_value(p),
                               **(spec.get("extra") or {})})
        assert reply["ok"], (p["id"], reply)
        st = status_to_dict(signalhound.status())
        if p["settle"]["policy"] == "immediate":
            # (rbw: snapped by the analyser, see its test) -- but the value
            # it reads back from must still exist
            assert lookup(st, p["read_path"]) is not None, p["id"]
            continue
        assert lookup(st, p["settle"]["key"]) is not None, p["id"]


def _probe_value(p):
    if p["type"] == "enum":
        return p["options"][-1]
    if p["type"] == "bool":
        return True
    lo, hi = p.get("min", 0.0), p.get("max", 1.0)
    # start / stop are the two ENDS of one sweep: probing both at the middle
    # would ask for stop == start, which the analyser rightly refuses
    frac = {"start": 0.25, "stop": 0.75}.get(p["id"], 0.5)
    return (lo + (hi - lo) * frac) * p.get("scale", 1.0)


def test_revision_ignores_values_but_follows_limits(signalhound):
    r0 = build_manifest(signalhound)["revision"]
    signalhound.set_ref_level(-30.0)                # a value, not a limit
    assert build_manifest(signalhound)["revision"] == r0
    signalhound.set_center(2e9)                     # moves span's maximum
    r1 = build_manifest(signalhound)["revision"]
    assert r1 != r0
    signalhound.set_rbw(10e3)                       # VBW's maximum and the bin count
    signalhound.step()
    assert build_manifest(signalhound)["revision"] != r1


def test_the_simulated_scene_is_absent_on_a_real_analyser():
    cfg = Config()
    real = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=FakeSaApi(device_type=4)), cfg)
    real.start(run=False)
    try:
        params = _params(real)
        assert {"trace", "tg_mode", "tg_cw_on"} <= set(params)
        assert not set(params) & {"tone_on", "dut_inserted", "tone_Hz"}
        assert build_manifest(real)["label"] == "Signal Hound SA124B"
        assert params["center"]["max"] > 12.0           # the SA124B's range
    finally:
        real.shutdown()


def test_rbw_settles_immediately_because_the_analyser_snaps_it():
    """150 kHz is stored as 100 kHz (the analyser has only 250 kHz above 100 kHz):
    an `echoes` policy would never match and a scan would sit out its timeout."""
    from signalhound.sim_system import build_sim_system
    cfg = Config()
    v, _ = build_sim_system(cfg, realtime=False, seed=1)
    v.start(run=False)
    try:
        d = {p["id"]: p for p in build_manifest(v)["parameters"]}
        assert d["rbw"]["settle"] == {"policy": "immediate"}
        v.set_rbw(150e3)
        assert v.status().rbw_Hz in (100e3, 250e3)
    finally:
        v.shutdown()


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


def _check_types(v):
    st = status_to_dict(v.status())
    for d in build_manifest(v)["parameters"]:
        if d.get("read_path") and d["kind"] in ("indicator", "control"):
            got = read_path(st, d["read_path"])
            assert _fits(d, got), f"{d['id']}: {got!r} does not fit {d}"
    return st


def test_every_tg_mode_is_an_option_and_every_value_fits(signalhound):
    """Walk the TG through ALL its modes (unknown at start, cw, parked, sweep)
    and both detectors: each status value fits its declared type, and tg_mode
    covers spectrum.TG_MODES exactly -- an enum value outside its options would
    be stored as "not measured"."""
    from signalhound.instruments import DETECTORS
    from signalhound.spectrum import TG_MODES
    p = _params(signalhound)
    assert p["tg_mode"]["type"] == "enum" and p["tg_mode"]["options"] == list(TG_MODES)
    assert p["detector"]["options"] == list(DETECTORS)
    seen = {_check_types(signalhound)["tg_mode"]}
    signalhound.tg_cw(True, 1e9, -20.0)
    seen.add(_check_types(signalhound)["tg_mode"])
    signalhound.tg_cw(False)
    seen.add(_check_types(signalhound)["tg_mode"])
    signalhound.tg_sweep_acquire(500e6, 1500e6, points=101, averages=1)
    seen.add(_check_types(signalhound)["tg_mode"])
    while signalhound.status().tg_acquiring:
        signalhound.step()
    for det in DETECTORS:
        signalhound.set_detector(det)
        signalhound.acquire()
        while signalhound.status().acquiring:
            signalhound.step()
        _check_types(signalhound)
    assert seen == set(TG_MODES)


def test_counters_promise_non_negative_ints(signalhound):
    p = _params(signalhound)
    for pid in ("points", "acq_id", "sweeps"):
        assert p[pid]["type"] == "int" and p[pid]["min"] == 0 and "max" not in p[pid]
