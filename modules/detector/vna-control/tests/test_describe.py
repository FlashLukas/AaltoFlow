"""The describe manifest: complete, consistent with status, and honest about
what changes it."""

import json

import pytest

from vna.config import Config
from vna.net.describe import build_manifest, read_path
from vna.net.protocol import status_to_dict
from vna.sim_system import build_sim_system


@pytest.fixture
def vna():
    cfg = Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    v, _ = build_sim_system(cfg, realtime=False, seed=1)
    v.start(run=False)
    yield v
    v.shutdown()


def _params(vna):
    return {p["id"]: p for p in build_manifest(vna)["parameters"]}


def test_manifest_is_json_and_ids_are_unique(vna):
    m = build_manifest(vna)
    json.dumps(m)                                   # no NaN, no numpy types
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids))
    assert m["module"] == "vna"


def test_every_read_path_resolves_in_status(vna):
    # a reference too, so the reference_* indicators have values to point at
    vna.take_reference()
    while vna.status().acquiring:
        vna.step()
    st = status_to_dict(vna.status())
    for p in build_manifest(vna)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None, p["id"]


def test_s_and_u_are_complex_array_detectors_read_by_command(vna):
    params = _params(vna)
    assert "s21" not in params                      # renamed to `s` (contract, 2026-09-16)
    s, u, ln = params["s"], params["u"], params["ln_ratio"]
    for d in (s, u, ln):
        assert d["dtype"] == "complex" and d["shape"] == ["freq"]
        dim = d["dims"][0]
        assert dim["coord_verb"] == "get_frequencies" and dim["unit"] == "GHz"
        assert dim["length"] == vna.cfg.sweep.points
        assert d["acquire"]["target_key"] == "acq_id"
    assert s["dims"] == u["dims"]                   # one shared frequency coordinate
    assert s["label"] == "S-parameter (S21)"
    assert s["read"] == {"verb": "get_trace", "key": "s",
                         "args": {"which": "sample", "quantity": "s"}}
    assert u["read"] == {"verb": "get_trace", "key": "u",
                         "args": {"which": "sample", "quantity": "u"}}
    assert u["label"] == "Permeability u = (S - S_ref)/S_ref"
    assert ln["read"] == {"verb": "get_trace", "key": "ln",
                          "args": {"which": "sample", "quantity": "ln"}}
    assert ln["label"] == "ln(S / S_ref)"
    assert s["dims"] == ln["dims"]                  # the same frequency coordinate again


def test_scalar_detectors_share_the_one_sweep(vna):
    params = _params(vna)
    ids = ("s", "u", "ln_ratio", "dip_freq", "dip_depth", "sweep_field", "sweep_angle",
           "sweep_field_ok")
    assert {params[k]["acquire"]["group"] for k in ids} == {"sweep"}
    assert params["sweep_angle"]["unit"] == "deg"
    assert params["sweep_angle"]["read_path"] == ["sample", "angle_deg"]


def test_reference_actions_carry_the_exact_wait_blocks(vna):
    params = _params(vna)
    take, clear = params["take_reference"], params["clear_reference"]
    assert take["kind"] == clear["kind"] == "action"
    assert take["wait"] == {
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": vna.cfg.acquisition.timeout_s,
    }
    assert clear["wait"] == {"ready": {"policy": "immediate"}}
    # front-panel-only buttons carry no wait block: a scan must not fire them
    assert "wait" not in params["acquire"] and "wait" not in params["abort"]
    assert params["reference_present"]["type"] == "bool"
    assert params["reference_field"]["unit"] == "mT"
    assert params["reference_angle"]["unit"] == "deg"


def test_action_verbs_exist(vna):
    from vna.net.service import VnaService
    svc = VnaService(vna)                           # not started: only _dispatch is used
    for p in build_manifest(vna)["parameters"]:
        if p["kind"] == "action":
            reply = svc._dispatch({"cmd": p["id"]})
            assert reply["ok"], (p["id"], reply)
    r = svc._dispatch({"cmd": "take_reference"})
    assert r["ok"] and isinstance(r["acq_id"], int)


def test_sparam_is_an_enum_control_and_names_the_s_label(vna):
    params = _params(vna)
    sp = params["sparam"]
    assert sp["type"] == "enum" and sp["options"] == ["S11", "S12", "S21", "S22"]
    assert sp["set"] == {"verb": "set_sparam", "arg": "sparam"}
    r0 = build_manifest(vna)["revision"]
    vna.set_sparam("S12")
    assert _params(vna)["s"]["label"] == "S-parameter (S12)"
    assert build_manifest(vna)["revision"] != r0     # clients re-fetch the label


def test_field_source_options_default_to_mag2d():
    assert Config().field.source == "mag2d"
    cfg = Config()
    cfg.field.source = "manual"
    v, _ = build_sim_system(cfg, realtime=False)
    p = {d["id"]: d for d in build_manifest(v)["parameters"]}
    # both vector magnets (mag2d and the calibrated parallel module), then the
    # 1-axis magnet, the DynaCool, then manual -- the order a GUI lists them in
    assert p["field_source"]["options"] == ["mag2d", "mag2dcal", "clMag", "ppms", "manual"]


def test_every_setter_verb_exists_and_settles_on_a_status_key(vna):
    from vna.net.service import VnaService
    svc = VnaService(vna)                           # not started: only _dispatch is used
    st = status_to_dict(vna.status())
    for p in build_manifest(vna)["parameters"]:
        spec = p.get("set")
        if not spec:
            continue
        reply = svc._dispatch({"cmd": spec["verb"], spec["arg"]: _probe_value(p),
                               **(spec.get("extra") or {})})
        assert reply["ok"], (p["id"], reply)
        assert p["settle"]["key"] in st, p["id"]


def _probe_value(p):
    if p["type"] == "enum":
        return p["options"][-1]
    if p["type"] == "bool":
        return True
    lo, hi = p.get("min", 0.0), p.get("max", 1.0)
    return ((lo + hi) / 2) * p.get("scale", 1.0)


def test_revision_ignores_values_but_follows_limits(vna):
    r0 = build_manifest(vna)["revision"]
    vna.set_manual_field(12.0)                      # a value, not a limit
    assert build_manifest(vna)["revision"] == r0
    vna.set_stop(4e9)                               # moves start's maximum
    assert build_manifest(vna)["revision"] != r0


def test_the_simulated_sample_group_is_absent_on_a_real_analyser():
    from vna.analyzer import Analyzer
    from vna.backends.pna import PnaVna
    cfg = Config()
    real = Analyzer(PnaVna(cfg), cfg)               # never opened: describe needs no hardware
    ids = {p["id"] for p in build_manifest(real)["parameters"]}
    assert {"s", "u", "take_reference", "sweep_angle", "field_source"} <= ids
    assert not ids & {"ms_mT", "alpha", "hk_mT", "geometry", "f_res_model"}
    assert build_manifest(real)["label"] == "Keysight PNA-X N5222A"
