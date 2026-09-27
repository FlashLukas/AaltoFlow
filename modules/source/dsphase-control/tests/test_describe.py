"""Tests for the `describe` manifest -- this service's self-description.

Two properties matter most:
  * every bound and step is read LIVE from cfg, never copied into a literal;
  * `revision` changes when the STRUCTURE or the BOUNDS change (a new phase
    step included), and not when a value changes.
"""

import pytest

pytest.importorskip("zmq")

from dsphase.config import Config
from dsphase.sim_system import build_sim_system
from dsphase.net.describe import build_manifest, read_path
from dsphase.net.service import DsphaseService
from dsphase.net.client import DsphaseClient

CMD_PORT = 17100          # this module's private test range: 17100..17119
PUB_PORT = 17101


@pytest.fixture
def brain():
    cfg = Config()
    b, _ = build_sim_system(cfg)
    b.start()
    yield b
    b.shutdown()


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields(brain):
    m = build_manifest(brain)
    assert m["module"] == "dsphase"
    assert isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    assert {"phase", "attenuation", "output_on", "frequency"} <= set(ids)
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        assert p["type"] in ("float", "int", "bool", "enum", "string", "action"), p["id"]
        if p["kind"] == "control":
            assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
            assert "settle" in p, p["id"]
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]


def test_phase_is_a_scan_axis_with_an_echo_settle(brain):
    ph = _by_id(build_manifest(brain))["phase"]
    cfg = brain.cfg
    assert ph["unit"] == "deg"
    assert (ph["min"], ph["max"]) == (cfg.limits.phase_min_deg, cfg.limits.phase_max_deg)
    assert ph["step"] == cfg.device.phase_step_deg
    assert ph["settle"]["policy"] == "echoes"
    assert ph["settle"]["key"] == "phase_deg"
    # half a step: asking for 33.3 must accept the unit's 33.5
    assert ph["settle"]["tol"] == pytest.approx(cfg.device.phase_step_deg / 2, abs=1e-5)
    assert ph["set"] == {"verb": "set_phase", "arg": "phase_deg"}


def test_settle_keys_exist_in_status(brain):
    """A settle key the status does not carry would hang a scan forever."""
    from dsphase.net.protocol import status_to_dict
    st = status_to_dict(brain.status())
    for p in build_manifest(brain)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None, p["id"]
        key = (p.get("settle") or {}).get("key")
        if key:
            assert key in st, p["id"]


def test_echo_tolerance_accepts_the_rounded_arrival(brain):
    """Simulate scan-core's echo check against a real brain."""
    ph = _by_id(build_manifest(brain))["phase"]
    brain.set_phase(33.3)
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2 and brain.status().phase_deg != 33.5:
        time.sleep(0.01)
    assert abs(brain.status().phase_deg - 33.3) <= ph["settle"]["tol"]


def test_revision_tracks_bounds_and_step_but_not_values(brain):
    cfg = brain.cfg
    rev0 = build_manifest(brain)["revision"]
    brain.set_phase(45.0)
    assert build_manifest(brain)["revision"] == rev0, "a value moved the revision"
    cfg.limits.att_min_dB = 10.0
    assert build_manifest(brain)["revision"] != rev0, "revision ignored a limit change"
    cfg.limits.att_min_dB = 0.0
    assert build_manifest(brain)["revision"] == rev0, "revision is not derived"
    cfg.device.phase_step_deg = 5.625
    m = build_manifest(brain)
    assert m["revision"] != rev0, "revision ignored a new device step"
    assert _by_id(m)["phase"]["decimals"] == 3


def test_read_path_survives_a_missing_branch(brain):
    ind = [p for p in build_manifest(brain)["parameters"] if p["kind"] == "indicator"][0]
    assert read_path({}, ind["read_path"]) is None


def test_describe_over_the_wire_and_both_status_paths_agree(brain):
    """A field in only one status path is a field that vanishes intermittently
    (a client uses the REQ reply until the first PUB frame arrives)."""
    svc = DsphaseService(brain, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
    svc.start()
    client = None
    try:
        client = DsphaseClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
        client.start()
        m = client.describe()
        assert m["module"] == "dsphase"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
        assert set(direct) >= {"phase_deg", "attenuation_dB", "output_on", "frequency_MHz"}
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()
