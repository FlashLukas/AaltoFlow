"""The describe manifest: complete, consistent with status, and honest about
what changes it."""

import json

import pytest

from shsna.config import Config
from shsna.net.describe import build_manifest, read_path
from shsna.net.protocol import status_to_dict
from shsna.net.service import ShsnaService
from shsna.sim_system import build_sim_system


@pytest.fixture
def shsna():
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 700e6, 1300e6, 201
    v, _ = build_sim_system(cfg, realtime=False, seed=1)
    v.start(run=False)
    yield v
    v.shutdown()


def _params(shsna):
    return {p["id"]: p for p in build_manifest(shsna)["parameters"]}


def _finish(v):
    while v.status().acquiring:
        v.step()


def test_manifest_is_json_and_ids_are_unique(shsna):
    m = build_manifest(shsna)
    json.dumps(m, allow_nan=False)                  # no NaN, no numpy types
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids))
    assert m["module"] == "shsna"


def test_every_read_path_resolves_in_status(shsna):
    shsna.take_reference()
    _finish(shsna)
    st = status_to_dict(shsna.status())
    for p in build_manifest(shsna)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None, p["id"]


def test_the_traces_are_array_detectors_read_by_command(shsna):
    params = _params(shsna)
    tx, raw = params["transmission"], params["raw"]
    for d in (tx, raw):
        assert d["type"] == "array" and d["dtype"] == "float" and d["shape"] == ["freq"]
        dim = d["dims"][0]
        assert dim["coord_verb"] == "get_frequencies" and dim["coord_key"] == "values_MHz"
        assert dim["unit"] == "MHz" and dim["length"] == 201
        assert d["acquire"]["target_key"] == "acq_id"
        assert d["read_path"] is None
    assert tx["dims"] == raw["dims"]                 # ONE shared frequency coordinate
    assert tx["unit"] == raw["unit"] == "dB"         # raw is rel. the TG output, not dBm
    assert tx["read"] == {"verb": "get_trace", "key": "transmission",
                          "args": {"which": "transmission", "source": "sample"}}
    assert raw["read"] == {"verb": "get_trace", "key": "raw",
                           "args": {"which": "raw", "source": "sample"}}


def test_all_detectors_share_one_acquisition_and_are_fetched(shsna):
    params = _params(shsna)
    ids = ("transmission", "raw", "peak_transmission", "peak_freq", "mean_transmission",
           "bw3", "raw_peak")
    assert {params[k]["acquire"]["group"] for k in ids} == {"sweep"}
    for k in ids[2:]:
        assert params[k]["read"]["verb"] == "get_result"
    assert params["peak_freq"]["scale"] == 1e6 and params["peak_freq"]["unit"] == "MHz"
    assert params["raw_peak"]["read"]["args"]["quantity"] == "raw"


def test_every_read_verb_answers_with_its_key(shsna):
    """What scan-core will do after the acquisition wait: call each `read`
    verb and take its key -- and each coord_verb once."""
    svc = ShsnaService(shsna)                       # not started: only _dispatch is used
    shsna.set_sim("dut_inserted", False)
    shsna.take_reference()
    _finish(shsna)
    shsna.set_sim("dut_inserted", True)
    acq = svc._dispatch({"cmd": "acquire"})
    _finish(shsna)
    for p in build_manifest(shsna)["parameters"]:
        spec = p.get("read")
        if not spec:
            continue
        reply = svc._dispatch({"cmd": spec["verb"], **spec["args"]})
        assert reply["ok"] and spec["key"] in reply, (p["id"], reply)
        json.dumps(reply, allow_nan=False)
    coord = svc._dispatch({"cmd": "get_frequencies"})
    assert len(coord["values_MHz"]) == 201 and coord["values_MHz"][0] == 700.0
    assert svc._dispatch({"cmd": "get_result"})["acq_id"] == acq["acq_id"]


def test_a_failed_acquisition_raises_at_the_read(shsna):
    """The detectors are FETCHED so that a failed sweep stops the scan with its
    reason, instead of a stale or empty number being filed."""
    svc = ShsnaService(shsna)
    shsna.backend.fail_next = "TG lost"
    svc._dispatch({"cmd": "acquire"})
    _finish(shsna)
    for p in build_manifest(shsna)["parameters"]:
        spec = p.get("read")
        if spec:
            reply = svc._dispatch({"cmd": spec["verb"], **spec["args"]})
            assert reply["ok"] is False and "TG lost" in reply["error"], p["id"]


def test_reference_actions_carry_the_exact_wait_blocks(shsna):
    params = _params(shsna)
    take, clear = params["take_reference"], params["clear_reference"]
    assert take["kind"] == clear["kind"] == "action"
    assert take["wait"] == {
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": shsna.cfg.acquisition.timeout_s,
        "check": {"key": "acq_error", "equals": ""},
    }
    assert clear["wait"] == {"ready": {"policy": "immediate"}}
    # front-panel-only buttons carry no wait block: a scan must not fire them
    assert "wait" not in params["acquire"] and "wait" not in params["abort"]


def test_the_wait_check_sees_a_failed_reference(shsna):
    """What scan-core's action runner does: wait for the id, then check."""
    svc = ShsnaService(shsna)
    check = _params(shsna)["take_reference"]["wait"]["check"]
    n = svc._dispatch({"cmd": "take_reference"})["acq_id"]
    _finish(shsna)
    st = svc.status_payload()
    assert st["acq_id"] == n and st[check["key"]] == check["equals"]      # success
    shsna.backend.fail_next = "no thru"
    n = svc._dispatch({"cmd": "take_reference"})["acq_id"]
    _finish(shsna)
    st = svc.status_payload()
    assert st["acq_id"] == n and not st["acquiring"]
    assert st[check["key"]] != check["equals"]                           # the routine raises


def test_action_verbs_exist(shsna):
    svc = ShsnaService(shsna)
    for p in build_manifest(shsna)["parameters"]:
        if p["kind"] == "action":
            reply = svc._dispatch({"cmd": p["id"]})
            assert reply["ok"], (p["id"], reply)


def test_every_setter_verb_exists_and_settles_on_a_status_key(shsna):
    svc = ShsnaService(shsna)
    for p in build_manifest(shsna)["parameters"]:
        spec = p.get("set")
        if not spec:
            continue
        reply = svc._dispatch({"cmd": spec["verb"], spec["arg"]: _probe_value(p),
                               **(spec.get("extra") or {})})
        assert reply["ok"], (p["id"], reply)
        st = status_to_dict(shsna.status())
        assert p["settle"]["key"] in st, p["id"]
        # the value set is the value echoed (within the declared tolerance)
        want = _probe_value(p)
        got = st[p["settle"]["key"]]
        if p["type"] in ("float", "int"):
            assert abs(got - want) <= p["settle"].get("tol", 0) + 1e-9, p["id"]
        else:
            assert got == want, p["id"]


def _probe_value(p):
    if p["type"] == "bool":
        return True
    lo, hi = p.get("min", 0.0), p.get("max", 1.0)
    v = ((lo + hi) / 2) * p.get("scale", 1.0)
    return int(round(v)) if p["type"] == "int" else v


def test_revision_follows_limits_and_the_known_grid():
    cfg = Config()
    v, _ = build_sim_system(cfg, realtime=False)
    v.start(run=False)
    r0 = build_manifest(v)["revision"]
    v.set_averages(4)                                 # a value, not a limit
    assert build_manifest(v)["revision"] == r0
    v.set_stop(4e9)                                   # moves start's maximum
    r1 = build_manifest(v)["revision"]
    assert r1 != r0
    v.set_points(101)                                 # the freq dim's length
    assert build_manifest(v)["revision"] != r1
    v.shutdown()


def test_the_grid_length_is_left_out_while_unknown():
    from shsna.analyzer import Analyzer
    from shsna.backends.remote_sa import RemoteSa
    cfg = Config()
    # scratch ports (suite rule): a signalhound service on the default 5587 --
    # the lab PC runs one -- must not be able to answer this test
    cfg.hardware.owner_cmd_port, cfg.hardware.owner_pub_port = 18098, 18099
    real = Analyzer(RemoteSa(cfg), cfg)               # never opened: describe needs no owner
    params = {p["id"]: p for p in build_manifest(real)["parameters"]}
    assert "length" not in params["transmission"]["dims"][0]
    assert not {"dut_inserted", "pad", "sim_tg_attached"} & set(params)
    assert {"transmission", "take_reference", "owner", "tg_attached"} <= set(params)
    assert "signalhound" in build_manifest(real)["label"]


def test_the_simulation_group_is_present_in_the_simulator(shsna):
    params = _params(shsna)
    assert params["dut_inserted"]["set"] == {"verb": "set_sim", "arg": "value",
                                             "extra": {"name": "dut_inserted"}}
    assert params["pad"]["unit"] == "dB"


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


def test_every_status_value_fits_its_declared_type(shsna):
    """Idle, mid-acquisition and after one: every indicator/control read from
    status has the type (and, for an indicator, the range) describe promises."""
    def check():
        st = status_to_dict(shsna.status())
        for d in build_manifest(shsna)["parameters"]:
            if d.get("read_path") and d["kind"] in ("indicator", "control"):
                v = read_path(st, d["read_path"])
                assert _fits(d, v), f"{d['id']}: {v!r} does not fit {d}"
    check()
    shsna.acquire()
    check()
    _finish(shsna)
    check()


def test_counters_promise_non_negative_ints(shsna):
    p = _params(shsna)
    for pid in ("acq_id", "sweeps", "reference_points"):
        assert p[pid]["type"] == "int" and p[pid]["min"] == 0 and "max" not in p[pid]


def test_tg_mode_is_an_enum_of_every_owner_mode(shsna):
    """The options are the owner's (signalhound) TG modes, and the brain maps
    anything else -- no owner yet, "", an unknown word -- to None, so no value
    is ever stored as "not measured" by accident."""
    from shsna import analyzer
    d = _params(shsna)["tg_mode"]
    assert d["type"] == "enum"
    assert d["options"] == ["unknown", "parked", "cw", "sweep"]
    for mode in analyzer.TG_MODES:
        assert analyzer._tg_mode(mode) == mode
    for other in ("", None, "--", "idle"):
        assert analyzer._tg_mode(other) is None
    assert analyzer._no_owner()["tg_mode"] is None
