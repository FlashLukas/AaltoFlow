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
    # an acquisition and a reference too, so the sample / reference paths exist
    signalhound.set_tg(True)
    signalhound.take_reference()
    while signalhound.status().acquiring:
        signalhound.step()
    signalhound.acquire()
    while signalhound.status().acquiring:
        signalhound.step()
    st = status_to_dict(signalhound.status())
    for p in build_manifest(signalhound)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None, p["id"]


def test_trace_and_transmission_are_real_array_detectors(signalhound):
    params = _params(signalhound)
    tr, tx = params["trace"], params["transmission"]
    for d in (tr, tx):
        assert d["type"] == "array" and d["dtype"] == "float" and d["shape"] == ["freq"]
        dim = d["dims"][0]
        assert dim["coord_verb"] == "get_frequencies" and dim["coord_key"] == "values_GHz"
        assert dim["length"] == signalhound.status().points
        assert d["acquire"]["target_key"] == "acq_id"
    assert tr["dims"] == tx["dims"]                 # one shared frequency coordinate
    assert tr["read"] == {"verb": "get_trace", "key": "trace",
                          "args": {"which": "sample", "quantity": "trace"}}
    assert tx["read"]["key"] == "transmission" and tx["unit"] == "dB"


def test_scalar_detectors_share_the_one_acquisition(signalhound):
    params = _params(signalhound)
    ids = ("trace", "transmission", "peak_freq", "peak_level", "noise_floor", "tx_center",
           "overloaded")
    assert {params[k]["acquire"]["group"] for k in ids} == {"sweep"}


def test_reference_actions_carry_the_exact_wait_blocks(signalhound):
    params = _params(signalhound)
    take, clear = params["take_reference"], params["clear_reference"]
    assert take["wait"] == {
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": signalhound.cfg.acquisition.timeout_s,
    }
    assert clear["wait"] == {"ready": {"policy": "immediate"}}
    assert "wait" not in params["acquire"] and "wait" not in params["abort"]


def test_action_verbs_exist(signalhound):
    from signalhound.net.service import SignalhoundService
    svc = SignalhoundService(signalhound)           # not started: only _dispatch is used
    signalhound.set_tg(True)                        # take_reference needs the TG
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
    return ((lo + hi) / 2) * p.get("scale", 1.0)


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


def test_turning_the_tg_on_changes_the_envelope(signalhound):
    signalhound.set_center(3e9)
    before = _params(signalhound)["center"]["max"]
    signalhound.set_tg(True)
    signalhound.step()
    after = _params(signalhound)["center"]["max"]
    assert before == pytest.approx(4.4, abs=1e-3) and after <= 4.4


def test_the_simulated_scene_is_absent_on_a_real_analyser():
    cfg = Config()
    real = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=FakeSaApi(device_type=4)), cfg)
    real.start(run=False)
    try:
        params = _params(real)
        assert {"trace", "take_reference", "tg_on"} <= set(params)
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
