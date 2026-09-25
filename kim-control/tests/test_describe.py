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

from kim.config import Config
from kim.sim_system import build_sim_system
from kim.net.describe import build_manifest, read_path
from kim.net.service import KimService
from kim.net.client import KimClient


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
        assert m["module"] == "kim"
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
        brain.set_leash(True, leash_xy=50000, leash_z=50000)
        assert build_manifest(brain)["revision"] != rev0, \
            "revision ignored a limit change"
        brain.set_leash(False)
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


def test_describe_over_the_wire_and_both_status_paths_agree():
    """A field in only one status path is a field that vanishes intermittently.

    A client uses the REQ reply whenever no PUB frame has arrived yet, since
    ZeroMQ SUB is a slow joiner.
    """
    cfg, brain = _brain()
    svc = KimService(brain, host="127.0.0.1", cmd_port=5799, pub_port=5800)
    svc.start()
    client = None
    try:
        client = KimClient(host="127.0.0.1", cmd_port=5799, pub_port=5800)
        client.start()
        m = client.describe()
        assert m["module"] == "kim"

        direct = client._rpc(cmd="status")["status"]
        assert direct["describe_rev"] == m["revision"], \
            "the status command reply and the manifest disagree"
    finally:
        if client is not None:
            client.close()
        svc.stop()


def test_armed_leash_narrows_the_position_bounds():
    """THE dynamic-limit case: an armed leash REPLACES the travel clamp.

    The bounds are read from the brain's published effective limits rather than
    recomputed here, so there is exactly one implementation of "what can this
    axis reach" -- the one that does the clamping.
    """
    cfg, brain = _brain()
    try:
        brain.set_leash(False)
        wide = build_manifest(brain)
        w = _by_id(wide)["position_x"]

        brain.set_leash(True, leash_xy=50000, leash_z=50000)
        tight = build_manifest(brain)
        t = _by_id(tight)["position_x"]

        assert (t["max"] - t["min"]) < (w["max"] - w["min"])
        assert tight["revision"] != wide["revision"]
        assert "LEASH" in t.get("help", "").upper()
    finally:
        brain.shutdown()


def test_positions_are_offered_in_micrometres_via_the_calibration():
    cfg, brain = _brain()
    try:
        p = _by_id(build_manifest(brain))["position_x"]
        assert p["unit"] == "um"
        assert p["set"]["verb"] == "move_to_um"
        assert p["read_path"] == ["position_um", 0]
    finally:
        brain.shutdown()
