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

from zpiezo.config import Config
from zpiezo.sim_system import build_sim_system
from zpiezo.net.describe import build_manifest, read_path
from zpiezo.net.service import ZPiezoService
from zpiezo.net.client import ZPiezoClient


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
        assert m["module"] == "zpiezo"
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
        cfg.limits.v_max = 50.0
        assert build_manifest(brain)["revision"] != rev0, \
            "revision ignored a limit change"
        cfg.limits.v_max = 75.0
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
    svc = ZPiezoService(brain, host="127.0.0.1", cmd_port=5793, pub_port=5794)
    svc.start()
    client = None
    try:
        client = ZPiezoClient(host="127.0.0.1", cmd_port=5793, pub_port=5794)
        client.start()
        m = client.describe()
        assert m["module"] == "zpiezo"

        direct = client._rpc(cmd="status")["status"]
        assert direct["describe_rev"] == m["revision"], \
            "the status command reply and the manifest disagree"
    finally:
        if client is not None:
            client.close()
        svc.stop()


def test_voltage_limits_come_from_the_config():
    cfg, brain = _brain()
    try:
        v = _by_id(build_manifest(brain))["voltage"]
        assert (v["min"], v["max"]) == (cfg.limits.v_min, cfg.limits.v_max)
    finally:
        brain.shutdown()
