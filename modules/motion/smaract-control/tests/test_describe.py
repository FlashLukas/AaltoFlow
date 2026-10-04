"""Tests for the `describe` manifest -- this service's self-description.

Two properties matter most and are easy to get wrong:

  * every bound is read LIVE from cfg (a manifest that restates a limit is a
    limit with two homes, and the wrong one does not announce itself)
  * `revision` changes when the STRUCTURE or the BOUNDS change, and not when a
    measured value changes
"""

import time

import pytest

pytest.importorskip("zmq")

from helpers import fast_cfg, make_brain, wait_until  # noqa: E402
from smaract.net.client import SmaractClient  # noqa: E402
from smaract.net.describe import build_manifest, read_path  # noqa: E402
from smaract.net.protocol import status_to_dict  # noqa: E402
from smaract.net.service import SmaractService  # noqa: E402


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields():
    brain, _ = make_brain()
    try:
        m = build_manifest(brain)
        assert m["module"] == "smaract"
        assert isinstance(m["revision"], int)
        ids = [p["id"] for p in m["parameters"]]
        assert len(ids) == len(set(ids)), "duplicate parameter ids"
        status = status_to_dict(brain.status())
        for p in m["parameters"]:
            assert p["kind"] in ("control", "indicator", "action"), p["id"]
            assert p["type"] in ("float", "int", "bool", "enum", "string", "action"), p["id"]
            if p["kind"] == "control":
                assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
                assert "settle" in p, p["id"]
            if p["kind"] in ("control", "indicator"):
                assert p["read_path"], p["id"]
                assert p["read_path"][0] in status, p["id"]   # it really resolves
            if p["kind"] == "action" and p.get("args"):
                for a in p["args"]:
                    assert "name" in a and "type" in a, p["id"]
    finally:
        brain.shutdown()


def test_bounds_follow_cfg_and_revision_tracks_them():
    cfg = fast_cfg()
    brain, _ = make_brain(cfg)
    try:
        m0 = build_manifest(brain)
        pos = _by_id(m0)["position"]
        assert (pos["min"], pos["max"]) == (cfg.limits.min_mm, cfg.limits.max_mm)
        vel = _by_id(m0)["velocity"]
        assert (vel["min"], vel["max"]) == brain.velocity_range()

        cfg.limits.max_mm = 50.0
        m1 = build_manifest(brain)
        assert _by_id(m1)["position"]["max"] == 50.0
        assert m1["revision"] != m0["revision"], "revision ignored a limit change"
        cfg.limits.max_mm = m0["parameters"][0]["max"]
        assert build_manifest(brain)["revision"] == m0["revision"]
    finally:
        brain.shutdown()


def test_revision_ignores_values():
    brain, _ = make_brain()
    try:
        rev0 = build_manifest(brain)["revision"]
        brain.move_by(0.2)
        assert wait_until(lambda: not brain.status().moving, 5.0)
        assert build_manifest(brain)["revision"] == rev0
    finally:
        brain.shutdown()


def test_settle_policies_and_stream():
    brain, _ = make_brain()
    try:
        p = _by_id(build_manifest(brain))
        s = p["position"]["settle"]
        assert s == {"policy": "adopt_then_flag", "setpoint_key": "target_mm",
                     "flag_key": "moving", "invert": True}
        assert p["position"]["stream"] == {"group": "position", "channel": "position"}
        assert p["velocity"]["settle"]["policy"] == "echoes"
        ref = p["find_reference"]
        assert ref["danger"] is True
        assert ref["wait"]["target_key"] == "ref_id"
        assert ref["wait"]["check"] == {"key": "referenced", "equals": True}
        assert "danger" not in p["stop"], "STOP must never ask for confirmation"
    finally:
        brain.shutdown()


def test_read_path_survives_a_missing_branch():
    brain, _ = make_brain()
    try:
        ind = [q for q in build_manifest(brain)["parameters"] if q["kind"] == "indicator"][0]
        assert read_path({}, ind["read_path"]) is None   # must not raise
    finally:
        brain.shutdown()


def test_describe_over_the_wire_and_both_status_paths_agree():
    """A field in only one status path is a field that vanishes intermittently."""
    brain, _ = make_brain(start=False)
    svc = SmaractService(brain, host="127.0.0.1", cmd_port=17182, pub_port=17183)
    svc.start()
    client = None
    try:
        client = SmaractClient(host="127.0.0.1", cmd_port=17182, pub_port=17183)
        client.start()
        m = client.describe()
        assert m["module"] == "smaract"
        direct = client._rpc(cmd="status")["status"]
        assert direct["describe_rev"] == m["revision"]
        # the action verbs a scan routine would send exist on the service
        for aid in ("find_reference", "stop", "set_zero", "clear_zero"):
            assert client._rpc(cmd=aid)["ok"], aid
        time.sleep(0.3)
        assert client.status().describe_rev == m["revision"]
    finally:
        if client is not None:
            client.close()
        svc.stop()


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
    brain, _ = make_brain()
    try:
        _check_types(build_manifest(brain), status_to_dict(brain.status()))
    finally:
        brain.shutdown()


def test_frequency_and_state_declarations():
    """max_frequency is read as an UNSIGNED int (min 0, nothing narrower);
    channel_state is a string because the SCU backend reports "code_<n>"
    for a state number it does not know -- an open set, not an enum."""
    from smaract.backends.base import CHANNEL_STATES
    brain, _ = make_brain()
    try:
        params = _by_id(build_manifest(brain))
        f = params["max_frequency"]
        assert f["type"] == "int" and f["min"] == 0 and "max" not in f
        assert params["channel_state"]["type"] == "string"
        assert len(CHANNEL_STATES) > 0
    finally:
        brain.shutdown()
