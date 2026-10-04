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

from ddr25.config import Config
from ddr25.sim_system import build_sim_system
from ddr25.net.describe import build_manifest, read_path
from ddr25.net.service import Ddr25Service
from ddr25.net.client import Ddr25Client


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
        assert m["module"] == "ddr25"
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
        cfg.limits.max_deg = 400.0
        assert build_manifest(brain)["revision"] != rev0,             "revision ignored a limit change"
        cfg.limits.max_deg = 720.0
        assert build_manifest(brain)["revision"] == rev0,             "revision is a counter, not derived from the manifest"
        brain.move_by(5.0)                   # a VALUE change: no new revision
        assert build_manifest(brain)["revision"] == rev0
    finally:
        brain.shutdown()


def test_angle_range_follows_the_wrap_policy():
    """The dynamic limit of this module: literal = the travel box, the
    modulo modes = one turn. Switching must change the revision."""
    cfg, brain = _brain()
    try:
        m0 = build_manifest(brain)
        a = _by_id(m0)["angle"]
        assert (a["min"], a["max"]) == (cfg.limits.min_deg, cfg.limits.max_deg)
        brain.set_wrap("shortest")
        m1 = build_manifest(brain)
        a = _by_id(m1)["angle"]
        assert (a["min"], a["max"]) == (0.0, 360.0)
        assert m1["revision"] != m0["revision"]
    finally:
        brain.shutdown()


def test_scan_contract_of_the_angle_and_actions():
    cfg, brain = _brain()
    try:
        p = _by_id(build_manifest(brain))
        a = p["angle"]
        assert a["set"] == {"verb": "move_to", "arg": "angle"}
        assert a["settle"]["policy"] == "adopt_then_flag"
        assert a["settle"]["setpoint_key"] == "target_deg"
        assert a["settle"]["flag_key"] == "moving" and a["settle"]["invert"] is True
        assert a["stream"] == {"group": "position", "channel": "angle"}
        assert p["velocity"]["max"] == cfg.limits.max_velocity
        assert p["velocity"]["settle"]["policy"] == "echoes"
        # actions are sent by their id: each id must be a verb the service knows
        home = p["home"]
        assert home["danger"] and home["wait"]["target_key"] == "home_id"
        assert home["wait"]["check"] == {"key": "homed", "equals": True}
        assert p["stop"]["danger"]
        # the move actions wait on the move NUMBER (see rotator.py docstring)
        for aid in ("move_by", "goto_angle"):
            w = p[aid]["wait"]
            assert w["target_key"] == "move_id"
            assert w["ready"]["setpoint_key"] == "move_id"
            assert w["ready"]["flag_key"] == "moving" and w["ready"]["invert"] is True
        assert p["move_by"]["args"][0]["default"] == cfg.motion.jog_step
    finally:
        brain.shutdown()


def test_every_status_key_named_by_the_manifest_exists():
    from ddr25.net.protocol import status_to_dict
    cfg, brain = _brain()
    try:
        st = status_to_dict(brain.status())
        for d in build_manifest(brain)["parameters"]:
            if d.get("read_path"):
                assert d["read_path"][0] in st, d["id"]
            for block in (d.get("settle") or {}, (d.get("wait") or {}).get("ready") or {}):
                for k in ("key", "setpoint_key", "flag_key"):
                    if k in block:
                        assert block[k] in st, (d["id"], block[k])
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
    svc = Ddr25Service(brain, host="127.0.0.1", cmd_port=17282, pub_port=17283)
    svc.start()
    client = None
    try:
        client = Ddr25Client(host="127.0.0.1", cmd_port=17282, pub_port=17283)
        client.start()
        m = client.describe()
        assert m["module"] == "ddr25"

        direct = client._rpc(cmd="status")["status"]
        assert direct["describe_rev"] == m["revision"], \
            "the status command reply and the manifest disagree"
    finally:
        if client is not None:
            client.close()
        svc.stop()


def test_every_wait_target_key_is_in_the_reply_and_in_status():
    """scan-core reads `target_key` out of the action's REPLY and then waits for
    the status field named by `ready.setpoint_key` to equal it -- both must
    exist, or the routine fails (or waits forever) on the lab PC."""
    from ddr25.net.protocol import status_to_dict
    cfg, brain = _brain()
    svc = Ddr25Service(brain, host="127.0.0.1", cmd_port=17290, pub_port=17291)
    try:
        brain.home()
        t0 = __import__("time").monotonic()
        while brain.status().moving:
            assert __import__("time").monotonic() - t0 < 30
            __import__("time").sleep(0.02)
        brain.store_angle(0)
        st = status_to_dict(brain.status())
        for d in build_manifest(brain)["parameters"]:
            w = d.get("wait") or {}
            if not w.get("target_key"):
                continue
            reply = svc._dispatch({"cmd": d["id"], **{a["name"]: a["default"]
                                                      for a in d.get("args", [])}})
            assert reply["ok"] and w["target_key"] in reply, d["id"]
            assert w["ready"]["setpoint_key"] in st, d["id"]
            brain.stop(immediate=True)
    finally:
        brain.shutdown()


def test_wrap_change_moves_describe_rev_in_the_next_frame():
    cfg, brain = _brain()
    svc = Ddr25Service(brain, host="127.0.0.1", cmd_port=17292, pub_port=17293)
    try:
        before = svc.status_payload()["describe_rev"]
        assert svc._dispatch({"cmd": "set_wrap", "wrap": "shortest"})["ok"]
        # no 1 s cache delay: the very next frame carries the new revision
        assert svc.status_payload()["describe_rev"] != before
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
    from ddr25.net.protocol import status_to_dict
    cfg, brain = _brain()
    try:
        _check_types(build_manifest(brain), status_to_dict(brain.status()))
    finally:
        brain.shutdown()


def test_wrap_options_are_every_policy_the_brain_reports():
    """Enumerated from the code: the options are config.WRAP_POLICIES, each
    one is accepted and reported, and a bad config value reads as one."""
    from ddr25.config import WRAP_POLICIES, wrap_policy
    from ddr25.net.protocol import status_to_dict
    cfg, brain = _brain()
    try:
        d = _by_id(build_manifest(brain))["wrap"]
        assert d["type"] == "enum" and d["options"] == list(WRAP_POLICIES)
        for pol in WRAP_POLICIES:
            brain.set_wrap(pol)
            assert status_to_dict(brain.status())["wrap"] in d["options"]
        bad = Config()
        bad.motion.wrap = "sideways"
        assert wrap_policy(bad) in d["options"]
    finally:
        brain.shutdown()
