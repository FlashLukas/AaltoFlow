"""Tests for the `describe` manifest -- the service's self-description.

The manifest exists so a client can build a control panel, or a scan registry,
for a module it knows nothing about. Two properties matter most and are easy to
get wrong:

  * every bound is read LIVE from cfg / the calibration, never copied into a
    literal here (a manifest that restates a limit is a limit with two homes,
    and one of them will be wrong)
  * `revision` changes when the STRUCTURE or the BOUNDS change, and not when a
    measured value changes -- otherwise clients either cache a stale panel or
    re-fetch several times a second
"""

import pytest

pytest.importorskip("zmq")

from clMag.config import Config
from clMag.sim_system import build_sim_system
from clMag.net.describe import build_manifest, read_path
from clMag.net.service import ClMagService
from clMag.net.client import ClMagClient


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields():
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    m = build_manifest(ctrl)

    assert m["module"] == "clMag"
    assert isinstance(m["revision"], int)
    params = m["parameters"]
    assert params, "manifest is empty"

    ids = [p["id"] for p in params]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"

    for p in params:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        assert p["type"] in ("float", "int", "bool", "enum", "string", "action")
        # every control must say HOW to set it, or a client cannot drive it
        if p["kind"] == "control":
            assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
        # an indicator a client cannot read is useless
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]


def test_limits_are_read_live_not_copied():
    """Change the config, and the manifest must follow without any other edit."""
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    assert _by_id(build_manifest(ctrl))["current"]["max"] == cfg.limits.current_max_A

    cfg.limits.current_max_A = 1.25
    p = _by_id(build_manifest(ctrl))["current"]
    assert (p["min"], p["max"]) == (-1.25, 1.25), \
        "current limits were copied into the manifest instead of looked up"


def test_field_range_follows_the_calibration():
    """clMag's field limits ARE the calibration range -- the trap this guards.

    With no calibration the service refuses every setpoint, so advertising a
    range would be a lie a control screen would happily draw a slider for.
    """
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)

    field = _by_id(build_manifest(ctrl))["field"]
    lo, hi = field["min"], field["max"]
    assert lo is not None and lo < 0 < hi

    saved, ctrl.calibration = ctrl.calibration, None
    try:
        field = _by_id(build_manifest(ctrl))["field"]
        assert field.get("min") is None and field.get("max") is None
        assert "NO CALIBRATION" in field.get("help", "").upper()
    finally:
        ctrl.calibration = saved


def test_revision_tracks_bounds_but_not_values():
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    rev0 = build_manifest(ctrl)["revision"]

    # a bound moves -> clients must be told
    cfg.limits.current_max_A = 2.0
    rev1 = build_manifest(ctrl)["revision"]
    assert rev1 != rev0, "revision ignored a limit change"

    # ...and it is derived, not a counter: restoring the state restores it
    cfg.limits.current_max_A = 3.0
    assert build_manifest(ctrl)["revision"] == rev0

    # a measured value moves -> clients must NOT be told, or they refetch forever
    m = build_manifest(ctrl)
    rev_before = m["revision"]
    ctrl.set_field(15.0)
    assert build_manifest(ctrl)["revision"] == rev_before


def test_read_path_resolves_nested_aux_channels():
    """AUX ids are nested and contain '/', which is why read_path is a list."""
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    params = _by_id(build_manifest(ctrl))

    ai = params["aux_ai1"]
    assert ai["read_path"] == ["aux", "ai", "Dev1/ai1"]

    status = {"aux": {"ai": {"Dev1/ai1": 1.23}}}
    assert read_path(status, ai["read_path"]) == 1.23
    # a missing branch must return None, not raise
    assert read_path({}, ai["read_path"]) is None


def test_actions_declare_their_arguments():
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    params = _by_id(build_manifest(ctrl))

    demag = params["demag"]
    assert demag["kind"] == "action"
    assert demag.get("danger") is True, "demag should ask for confirmation"
    names = {a["name"] for a in demag["args"]}
    assert names == {"amplitude_A"}
    amp = demag["args"][0]
    assert amp["max"] == cfg.limits.current_max_A     # again: looked up, not typed


def test_describe_over_the_wire_and_revision_in_status():
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5781, pub_port=5782)
    svc.start()
    client = None
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5781, pub_port=5782)
        client.start()

        m = client.describe()
        assert m["module"] == "clMag"
        assert _by_id(m)["field"]["set"]["verb"] == "set_field"

        # the status stream carries the revision, so a client can notice a
        # stale manifest without re-fetching the whole thing every poll
        st = client.status()
        assert st.describe_rev == m["revision"]

        # BOTH status paths must carry it. A client uses the REQ reply whenever
        # no PUB frame has arrived yet (SUB is a slow joiner), so a field in only
        # one of them is a field that vanishes intermittently -- which is how
        # this was broken when describe_rev was first added to the publisher
        # alone.
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"],             "the status command reply and the PUB frame disagree"
    finally:
        if client:
            client.shutdown()
        svc.stop()
