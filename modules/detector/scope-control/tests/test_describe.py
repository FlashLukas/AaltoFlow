"""The `describe` manifest: shape, keys that exist, settle rules, the acquire
block, what moves the revision, and that every value fits its type."""

import time

import pytest

pytest.importorskip("zmq")

from scope.config import Config
from scope.sim_system import build_sim_system
from scope.net.describe import build_manifest, read_path, acquisition_timeout_s
from scope.net.protocol import status_to_dict


@pytest.fixture
def scope():
    cfg = Config()
    s, _ = build_sim_system(cfg, seed=4)
    s.start()
    yield s
    s.shutdown()


def _by_id(m):
    return {p["id"]: p for p in m["parameters"]}


def _latch(s):
    s.set_averages(2)
    n = s.acquire()
    t_end = time.monotonic() + 10
    while time.monotonic() < t_end:
        st = s.status()
        if st["acq_id"] == n and not st["acquiring"]:
            return
        time.sleep(0.02)
    raise AssertionError("no sample")


def test_shape_and_required_fields(scope):
    m = build_manifest(scope)
    assert m["module"] == "scope" and isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids))
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        if p["kind"] == "control":
            assert "set" in p and "settle" in p, p["id"]
        if p["kind"] == "indicator" and p["type"] != "array":
            assert p["read_path"], p["id"]


def test_every_key_exists_in_status(scope):
    _latch(scope)
    st = status_to_dict(scope.status())
    for p in build_manifest(scope)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"][:1]) is not None, p["id"]
        for blk in (p.get("settle") or {}, (p.get("acquire") or {}).get("ready") or {}):
            for k in ("key", "setpoint_key", "flag_key"):
                if k in blk:
                    assert blk[k] in st, (p["id"], blk[k])


def test_scope_settings_echo_what_was_asked(scope):
    by = _by_id(build_manifest(scope))
    assert by["ch1_vdiv"]["settle"] == {"policy": "adopt_then_flag",
                                        "setpoint_key": "ch1_vdiv_V_set",
                                        "flag_key": "settings_settled"}
    assert by["ch1_vdiv"]["read_path"] == ["ch1_vdiv_V"]


def test_trace_and_scalar_detectors_share_one_acquire(scope):
    by = _by_id(build_manifest(scope))
    tr = by["ch1"]
    assert tr["type"] == "array" and tr["dims"][0]["name"] == "time"
    assert tr["dims"][0]["length"] == scope.cfg.acquisition.points
    assert tr["read"]["verb"] == "get_trace" and tr["read"]["args"] == {"which": "sample"}
    groups = {by[i]["acquire"]["group"] for i in ("ch1", "ch2", "ch1_mean", "loop_hc", "phase_21")}
    assert groups == {"scope"}
    assert by["loop_hc"]["acquire"]["target_key"] == "acq_id"
    assert by["loop_hc"]["read_path"] == ["sample", "loop", "hc"]


def test_revision_follows_shape_not_values(scope):
    rev0 = build_manifest(scope)["revision"]
    scope.set_filter(lowpass_Hz=1000.0)       # a VALUE
    assert build_manifest(scope)["revision"] == rev0
    scope.set_keep_raw(True)                  # new detectors
    by = _by_id(build_manifest(scope))
    assert "ch1_raw" in by and build_manifest(scope)["revision"] != rev0
    scope.set_keep_raw(False)
    scope.set_points(500)                     # trace length
    assert _by_id(build_manifest(scope))["ch1"]["dims"][0]["length"] == 500
    scope.set_physical("ch1", unit="mT")      # units
    by = _by_id(build_manifest(scope))
    assert by["ch1"]["unit"] == "mT" and by["loop_hc"]["unit"] == "mT"


def test_timeout_grows_with_averages(scope):
    cfg = scope.cfg
    cfg.acquisition.averages = 2
    short = acquisition_timeout_s(cfg)
    cfg.acquisition.averages = 500
    long = acquisition_timeout_s(cfg)
    assert long > 500 / cfg.acquisition.min_trigger_hz > short


def test_every_value_fits_its_type(scope):
    _latch(scope)
    st = status_to_dict(scope.status())
    for d in build_manifest(scope)["parameters"]:
        if d["kind"] == "action" or d["type"] == "array" or not d.get("read_path"):
            continue
        v = read_path(st, d["read_path"])
        if v is None:
            continue
        t = d["type"]
        if t == "bool":
            assert isinstance(v, bool), d["id"]
        elif t in ("float", "int"):
            assert isinstance(v, (int, float)) and not isinstance(v, bool), (d["id"], v)
        elif t == "enum":
            assert v in d["options"], (d["id"], v)
        elif t == "string":
            assert isinstance(v, str), d["id"]
