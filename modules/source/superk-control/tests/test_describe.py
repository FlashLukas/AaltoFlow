"""Tests for the `describe` manifest -- this service's self-description.

  * every bound is read LIVE from cfg / the active crystal, never a literal
  * `revision` changes when the STRUCTURE or the BOUNDS change (a crystal
    switch!), and not when a measured value changes
  * the class 4 safety shows: emission ON is a danger action
"""

import pytest

pytest.importorskip("zmq")

from superk.config import Config, N_LINES
from superk.sim_system import build_sim_system
from superk.net.describe import build_manifest, read_path
from superk.net.protocol import status_to_dict
from superk.net.service import SuperkService
from superk.net.client import SuperkClient

CMD_PORT, PUB_PORT = 17340, 17341          # this module's test range: 17340..17359


@pytest.fixture
def laser():
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    yield brain
    brain.shutdown()


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields(laser):
    m = build_manifest(laser)
    assert m["module"] == "superk"
    assert isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    st = status_to_dict(laser.status())
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        if p["kind"] == "control":
            assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
        if p["kind"] in ("control", "indicator"):
            assert p["read_path"], p["id"]
            assert read_path(st, p["read_path"]) is not None, p["id"]
        if p["kind"] == "action":
            assert "wait" in p, p["id"]


def test_every_line_is_a_flat_control_with_an_indexed_settle(laser):
    d = _by_id(build_manifest(laser))
    for n in range(1, N_LINES + 1):
        wl, amp = d[f"wavelength_{n}"], d[f"amplitude_{n}"]
        assert wl["set"]["extra"] == {"line": n}
        # a list-valued status key needs an index (gotcha #16)
        assert wl["settle"]["index"] == n - 1
        assert amp["settle"]["index"] == n - 1
    assert d["wavelength_1"]["plottable"] is True


def test_wavelength_limits_follow_the_crystal_and_revision_moves(laser):
    m0 = build_manifest(laser)
    wl = _by_id(m0)["wavelength_1"]
    assert (wl["min"], wl["max"]) == laser.wavelength_range() == (500.0, 900.0)
    laser.set_filter("IR")
    m1 = build_manifest(laser)
    wl = _by_id(m1)["wavelength_3"]
    assert (wl["min"], wl["max"]) == (1100.0, 2000.0)
    assert m1["revision"] != m0["revision"]
    laser.set_filter("VIS-nIR")
    assert build_manifest(laser)["revision"] == m0["revision"], \
        "revision is a counter, not derived from the manifest"


def test_revision_ignores_values(laser):
    rev0 = build_manifest(laser)["revision"]
    laser.set_power(20.0)
    laser.set_wavelength(1, 700.0)
    assert build_manifest(laser)["revision"] == rev0


def test_power_limits_come_from_the_config(laser):
    p = _by_id(build_manifest(laser))["power"]
    assert (p["min"], p["max"]) == (laser.cfg.limits.power_min_pct,
                                    laser.cfg.limits.power_max_pct)
    laser.cfg.limits.power_max_pct = 30.0
    assert _by_id(build_manifest(laser))["power"]["max"] == 30.0


def test_emission_on_is_a_danger_action_that_waits_for_the_laser(laser):
    d = _by_id(build_manifest(laser))
    on = d["emission_on"]
    assert on["kind"] == "action" and on.get("danger") is True
    assert on["wait"]["ready"] == {"policy": "flag_only", "key": "emission_on"}
    assert d["emission_off"]["wait"]["ready"]["invert"] is True
    assert "danger" not in d["emission_off"]
    # emission is NOT a sweepable control
    assert all(p["kind"] != "control" or "emission" not in p["id"]
               for p in build_manifest(laser)["parameters"])


def test_describe_over_the_wire_and_both_status_paths_agree(laser):
    svc = SuperkService(laser, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
    laser.shutdown()                        # the service starts it again
    svc.start()
    client = None
    try:
        client = SuperkClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
        client.start()
        m = client.describe()
        assert m["module"] == "superk"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
        # a crystal switch over the wire moves the revision the service reports
        client.set_filter("nIR2")
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] != m["revision"]
        assert direct["describe_rev"] == client.describe()["revision"]
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()


# ---- declared types = how scan-core STORES each value (developer notes 4b) ----

def _fits(d, v):
    """True if status value v fits descriptor d's declared type (None always
    fits: "not measured"). The same promise scan-core keeps when it stores."""
    if v is None:
        return True
    t = d["type"]
    if t == "bool":
        return isinstance(v, bool)
    if t == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            return False
        lo, hi = d.get("min"), d.get("max")
        if d.get("bits") is not None:
            lo, hi = 0, 2 ** d["bits"] - 1
        if d["kind"] == "indicator":
            return (lo is None or v >= lo) and (hi is None or v <= hi)
        return True
    if t == "enum":
        return v in d["options"]
    if t == "string":
        return isinstance(v, str)
    if t == "float":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return True


def test_every_status_value_fits_its_declared_type(laser):
    for name in laser.filter_names():           # every crystal, every range
        try:
            laser.set_filter(name)
        except Exception:                       # other housing: cable by hand
            continue
        m = build_manifest(laser)
        st = status_to_dict(laser.status())
        for p in m["parameters"]:
            if p.get("read_path"):
                v = read_path(st, p["read_path"])
                assert _fits(p, v), (name, p["id"], v)


def test_emission_state_enum_lists_every_state_the_code_can_produce():
    """Enumerated from the SOURCE, not from one snapshot: every literal the
    brain assigns to emission_state must be an option."""
    import inspect
    import re
    from superk import laser as L
    src = inspect.getsource(L)
    produced = set(re.findall(r'emission_state\s*(?::\s*str\s*)?=\s*"([^"]+)"', src))
    assert produced, "pattern found nothing -- update this test"
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    opts = _by_id(build_manifest(brain))["emission_state"]["options"]
    assert produced <= set(opts), produced - set(opts)
    assert list(opts) == list(L.EMISSION_STATES)


def test_filter_is_none_not_empty_when_unknown_and_crystal_is_one_byte(laser):
    from superk.laser import Status
    assert Status().filter is None, "'' is not an enum option; report None"
    d = _by_id(build_manifest(laser))
    assert d["filter"]["type"] == "enum"
    assert status_to_dict(laser.status())["filter"] in d["filter"]["options"]
    c = d["crystal_no"]
    assert c["type"] == "int" and c["bits"] == 8 and "min" not in c
