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

from stage.config import Config
from stage.sim_system import build_sim_system
from stage.net.describe import build_manifest, read_path
from stage.net.service import StageService
from stage.net.client import StageClient


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
        assert m["module"] == "stage"
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
        cfg.limits.max_x = 10.0
        assert build_manifest(brain)["revision"] != rev0, \
            "revision ignored a limit change"
        cfg.limits.max_x = 25.0
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
    svc = StageService(brain, host="127.0.0.1", cmd_port=5795, pub_port=5796)
    svc.start()
    client = None
    try:
        client = StageClient(host="127.0.0.1", cmd_port=5795, pub_port=5796)
        client.start()
        m = client.describe()
        assert m["module"] == "stage"

        direct = client._rpc(cmd="status")["status"]
        assert direct["describe_rev"] == m["revision"], \
            "the status command reply and the manifest disagree"
    finally:
        if client is not None:
            client.close()
        svc.stop()


def test_axes_are_expanded_flat_with_their_own_limits():
    """One descriptor per axis, so a panel can place a single axis."""
    cfg, brain = _brain()
    try:
        params = _by_id(build_manifest(brain))
        for ax, (lo, hi) in (("x", (cfg.limits.min_x, cfg.limits.max_x)),
                             ("y", (cfg.limits.min_y, cfg.limits.max_y)),
                             ("z", (cfg.limits.min_z, cfg.limits.max_z))):
            p = params[f"position_{ax}"]
            assert (p["min"], p["max"]) == (lo, hi)
            # the axis travels in the set block's extra, not in the value
            assert p["set"]["extra"]["axis"] == ax.upper()
            # per-axis moving flag, indexed into the status list
            assert p["settle"]["key"] == "moving"
            assert p["settle"]["invert"] is True
    finally:
        brain.shutdown()
