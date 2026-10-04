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


# --------------------------------------------------------------------------- #
# Declared TYPES (2026-10-04): scan-core STORES every recorded value in the
# type its descriptor declares (INSTRUMENT_MODULE_GUIDE.md 6b, developer
# notes 4b). An int outside an indicator's min/max STOPS a scan; an enum
# value that is not one of the options is lost as "not measured".
# --------------------------------------------------------------------------- #
def _type_problem(d, v):
    """Why status value `v` does not fit descriptor `d`, or "" (the same rule
    as tools/check_modules.py and scan_core/storage.py). None always fits."""
    items = v if isinstance(v, list) else [v]
    for x in items:
        if x is None:
            continue
        t = d["type"]
        if t == "bool" and not isinstance(x, bool):
            return f"declared bool, reads {x!r}"
        if t == "int":
            if isinstance(x, bool) or not isinstance(x, (int, float)) or x != int(x):
                return f"declared int, reads {x!r}"
            if d["kind"] == "indicator":
                lo, hi = d.get("min"), d.get("max")
                if d.get("bits") is not None:
                    lo, hi = 0, 2 ** int(d["bits"]) - 1
                if (lo is not None and x < lo) or (hi is not None and x > hi):
                    return f"reads {x!r}, outside [{lo}, {hi}]"
        if t == "enum" and x not in (d.get("options") or []):
            return f"reads {x!r}, not one of {d.get('options')}"
        if t == "string" and not isinstance(x, str):
            return f"declared string, reads {x!r}"
        if t == "float" and (isinstance(x, (bool, str)) or not isinstance(x, (int, float))):
            return f"declared float, reads {x!r}"
    return ""


def _check_types(manifest, status):
    """Every readable descriptor's status value fits its declared type."""
    bad = []
    for d in manifest["parameters"]:
        if d["kind"] in ("indicator", "control") and d.get("read_path"):
            why = _type_problem(d, read_path(status, d["read_path"]))
            if why:
                bad.append(f"{d['id']}: {why}")
    assert not bad, bad


def test_declared_types_fit_status():
    import time
    from camera.camera import status_to_dict
    cfg, brain = _brain()
    try:
        for _ in range(3):
            _check_types(build_manifest(brain), status_to_dict(brain.status()))
            time.sleep(0.1)
    finally:
        brain.shutdown()


def test_objective_options_include_the_reported_name():
    """The objective is an enum: the name status reports must be an option,
    also when it is not in objectives.ini, and when the table is empty
    (scan-core would otherwise store it as "not measured")."""
    from camera.camera import status_to_dict
    cfg, brain = _brain()
    try:
        params = _by_id(build_manifest(brain))
        name = status_to_dict(brain.status())["objective_name"]
        assert name in params["objective"]["options"]
        # a name the table does not know (an .ini from another table)
        brain.cfg.image.objective_name = "999x - not in the table"
        opts = _by_id(build_manifest(brain))["objective"]["options"]
        assert "999x - not in the table" in opts
        assert len(opts) == len(set(opts))
        # an empty objectives table
        brain._objectives = {}
        opts = _by_id(build_manifest(brain))["objective"]["options"]
        assert opts == ["999x - not in the table"]
    finally:
        brain.shutdown()


def test_spot_bit_depth_range_covers_every_pixel_format():
    """[min, max] of spot_bit_depth must hold every depth the code can
    report: backends.ids.bit_depth() of any pixel format, and 8 when there
    is no deep frame (camera._spot_source)."""
    from camera.backends.ids import bit_depth
    cfg, brain = _brain()
    try:
        d = _by_id(build_manifest(brain))["spot_bit_depth"]
        formats = ["", "Mono8", "Mono10", "Mono10p", "Mono12", "Mono12p",
                   "Mono12g24IDS", "Mono14", "Mono16", "Mono32", "Mono1",
                   "RGB8", "BayerRG12", None]
        depths = {bit_depth(f) for f in formats} | {8}
        for b in sorted(depths):
            assert d["min"] <= b <= d["max"], b
        assert _by_id(build_manifest(brain))["af_id"]["min"] == 0
    finally:
        brain.shutdown()
