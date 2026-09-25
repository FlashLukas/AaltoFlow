"""Tests for the `describe` manifest -- this service's self-description.

Two properties matter most and are easy to get wrong:

  * every bound is read LIVE from cfg or from status, never copied into a
    literal (a manifest that restates a limit is a limit with two homes, and
    the wrong one does not announce itself -- it just draws a slider with the
    wrong range)
  * `revision` changes when the STRUCTURE or the BOUNDS change, and not when a
    measured value changes, or clients either cache a stale panel or re-fetch
    several times a second
"""

import pytest

pytest.importorskip("zmq")

from camera.config import Config
from camera.sim_system import build_sim_system
from camera.net.describe import build_manifest, read_path


def _brain():
    cfg = Config()
    built = build_sim_system(cfg)
    brain = built[0] if isinstance(built, tuple) else built
    brain.start()
    return cfg, brain


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields():
    cfg, brain = _brain()
    try:
        m = build_manifest(brain)
        assert m["module"] == "camera"
        assert isinstance(m["revision"], int)
        assert m["parameters"], "manifest is empty"

        ids = [p["id"] for p in m["parameters"]]
        assert len(ids) == len(set(ids)), "duplicate parameter ids"

        for p in m["parameters"]:
            assert p["kind"] in ("control", "indicator", "action"), p["id"]
            assert p["type"] in ("float", "int", "bool", "enum", "string",
                                 "action"), p["id"]
            # a control a client cannot drive, or an indicator it cannot read,
            # is dead weight in a control screen
            if p["kind"] == "control":
                assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
            if p["kind"] == "indicator":
                assert p["read_path"], p["id"]
            if p["kind"] == "action" and p.get("args"):
                for a in p["args"]:
                    assert "name" in a and "type" in a, p["id"]
    finally:
        brain.shutdown()


def test_revision_tracks_bounds_but_not_values():
    cfg, brain = _brain()
    try:
        rev0 = build_manifest(brain)["revision"]
        cfg.limits.z_max_v = 50.0
        assert build_manifest(brain)["revision"] != rev0, \
            "revision ignored a limit change"
        cfg.limits.z_max_v = 75.0
        assert build_manifest(brain)["revision"] == rev0, \
            "revision is a counter, not derived from the manifest"
    finally:
        brain.shutdown()


def test_read_path_resolves_and_survives_a_missing_branch():
    cfg, brain = _brain()
    try:
        m = build_manifest(brain)
        ind = [p for p in m["parameters"] if p["kind"] == "indicator"][0]
        assert read_path({}, ind["read_path"]) is None   # must not raise
    finally:
        brain.shutdown()


def test_z_limits_come_from_the_config():
    cfg, brain = _brain()
    try:
        z = _by_id(build_manifest(brain))["z"]
        assert (z["min"], z["max"]) == (cfg.limits.z_min_v, cfg.limits.z_max_v)
    finally:
        brain.shutdown()


def test_xy_is_an_action_not_a_per_axis_control():
    """move_xy takes both axes at once, so there is no per-axis verb to point a
    Settable at -- and inventing one would give the rig two competing paths to
    the same motors, since this module drives XY through piezo-control."""
    cfg, brain = _brain()
    try:
        params = _by_id(build_manifest(brain))
        assert "position_x" not in params
        assert params["stage_x"]["kind"] == "indicator"

        mv = params["move_xy"]
        assert mv["kind"] == "action"
        assert {a["name"] for a in mv["args"]} == {"x", "y"}
    finally:
        brain.shutdown()


def test_the_two_vision_loops_are_boolean_controls():
    cfg, brain = _brain()
    try:
        params = _by_id(build_manifest(brain))
        for pid in ("tracking", "stabilize"):
            assert params[pid]["type"] == "bool"
            assert params[pid]["kind"] == "control"
    finally:
        brain.shutdown()
