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

from piezo.config import Config
from piezo.sim_system import build_sim_system
from piezo.net.describe import build_manifest, read_path
from piezo.net.service import PiezoService
from piezo.net.client import PiezoClient


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
        assert m["module"] == "piezo"
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
        # NB: use a bound that does NOT depend on the loop mode. Changing
        # travel_max_ol here would also mean toggling closed_loop, and the
        # restore would then have to put the mode back too -- an easy way to
        # write a test that fails for a reason unrelated to what it checks.
        cfg.limits.max_velocity = 500.0
        assert build_manifest(brain)["revision"] != rev0, \
            "revision ignored a limit change"
        cfg.limits.max_velocity = 2000.0
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
    svc = PiezoService(brain, host="127.0.0.1", cmd_port=5797, pub_port=5798)
    svc.start()
    client = None
    try:
        client = PiezoClient(host="127.0.0.1", cmd_port=5797, pub_port=5798)
        client.start()
        m = client.describe()
        assert m["module"] == "piezo"

        direct = client._rpc(cmd="status")["status"]
        assert direct["describe_rev"] == m["revision"], \
            "the status command reply and the manifest disagree"
    finally:
        if client is not None:
            client.close()
        svc.stop()


def test_travel_ceiling_follows_the_loop_mode():
    """THE dynamic-limit case: CL travel is smaller than OL, and the manifest
    must say so -- otherwise a control screen offers travel the stage no longer
    has, and the move is silently clamped."""
    cfg, brain = _brain()
    try:
        brain.set_closed_loop(0, False)
        m_ol = build_manifest(brain)
        ol = _by_id(m_ol)["position_x"]["max"]

        brain.set_closed_loop(0, True)
        m_cl = build_manifest(brain)
        cl = _by_id(m_cl)["position_x"]["max"]

        assert cl < ol, "closed-loop travel should be the smaller one"
        assert cl == pytest.approx(cfg.limits.travel_max_cl)
        assert ol == pytest.approx(cfg.limits.travel_max_ol)
        assert m_cl["revision"] != m_ol["revision"], \
            "a control screen would never know to redraw its slider"
    finally:
        brain.shutdown()


def test_ramp_mode_is_an_enum_with_its_options():
    cfg, brain = _brain()
    try:
        p = _by_id(build_manifest(brain))["ramp_mode"]
        assert p["type"] == "enum"
        assert set(p["options"]) == {"hardware", "software", "off"}
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
    from piezo.net.protocol import status_to_dict
    cfg, brain = _brain()
    try:
        _check_types(build_manifest(brain), status_to_dict(brain.status()))
    finally:
        brain.shutdown()


def test_ramp_mode_options_are_every_mode_the_brain_reports():
    """Enumerated from the code, not from one snapshot: the options are
    config.RAMP_MODES, every one of them is accepted and echoed, and a bad
    value in the config is forced back into the list."""
    import time
    from piezo.config import RAMP_MODES
    from piezo.net.protocol import status_to_dict
    cfg, brain = _brain()
    try:
        d = _by_id(build_manifest(brain))["ramp_mode"]
        assert d["type"] == "enum" and d["options"] == list(RAMP_MODES)
        for mode in RAMP_MODES:
            brain.set_ramp_mode(mode)
            time.sleep(0.05)
            assert status_to_dict(brain.status())["ramp_mode"] in d["options"]
        with pytest.raises(ValueError):
            brain.set_ramp_mode("turbo")
        # a remote set_config with a bad value is forced back into the list
        brain.cfg.motion.ramp_mode = "turbo"
        brain.apply_config()
        assert brain.cfg.motion.ramp_mode in d["options"]
    finally:
        brain.shutdown()
