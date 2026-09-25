"""The `describe` manifest -- this service's self-description.

Beyond the checks every module has (bounds follow cfg, revision tracks bounds
but not values, both status paths agree), a lock-in has two of its own:

  * every scan detector carries the SAME acquire group, whose wait is keyed on
    the id the trigger returns -- otherwise each detector costs its own settle
    time, or the wait is fooled by the previous point's status
  * `freq1` flips between control and indicator with the reference mode
"""

import pytest

pytest.importorskip("zmq")

from hf2.config import Config
from hf2.sim_system import build_sim_system
from hf2.net.describe import build_manifest, read_path
from hf2.net.protocol import status_to_dict
from hf2.net.service import Hf2Service
from hf2.net.client import Hf2Client


@pytest.fixture
def brain():
    cfg = Config()
    li, _ = build_sim_system(cfg, seed=4)
    li.start()
    yield cfg, li
    li.shutdown()


def _by_id(m):
    return {p["id"]: p for p in m["parameters"]}


def test_manifest_shape_and_required_fields(brain):
    cfg, li = brain
    m = build_manifest(li)
    assert m["module"] == "hf2" and isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        if p["kind"] == "control":
            assert "verb" in p["set"] and "arg" in p["set"], p["id"]
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]


def test_every_read_path_resolves_against_a_real_status(brain):
    cfg, li = brain
    import time
    li.acquire()
    deadline = time.monotonic() + 3
    while li.status().acquiring and time.monotonic() < deadline:
        time.sleep(0.02)
    st = status_to_dict(li.status())
    for p in build_manifest(li)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None, p["id"]


def test_scan_detectors_share_one_acquisition_keyed_on_the_trigger_reply(brain):
    cfg, li = brain
    m = _by_id(build_manifest(li))
    scan_ids = ["x1", "y1", "r1", "theta1", "x2", "y2", "r2", "theta2", "aux1", "aux2"]
    blocks = [m[i]["acquire"] for i in scan_ids]
    assert all(b == blocks[0] for b in blocks)
    b = blocks[0]
    assert b["trigger_verb"] == "acquire" and b["target_key"] == "acq_id"
    assert b["ready"]["policy"] == "adopt_then_flag"
    assert b["ready"]["setpoint_key"] == "acq_id"
    # live values are NOT scan-safe and must not pretend to be
    assert "acquire" not in m["live_r1"]


def test_frequency_is_a_control_only_on_internal_reference(brain):
    cfg, li = brain
    # default: frequency set from software -> a scannable control
    m = _by_id(build_manifest(li))
    assert m["freq1"]["kind"] == "control"
    assert m["freq1"]["max"] == cfg.limits.freq_max_Hz
    assert "locked1" not in m
    rev_int = build_manifest(li)["revision"]
    li.set_reference(1, "external")
    m = _by_id(build_manifest(li))
    assert m["freq1"]["kind"] == "indicator" and "locked1" in m
    assert build_manifest(li)["revision"] != rev_int


def test_limits_come_from_the_config_and_revision_follows(brain):
    cfg, li = brain
    m = build_manifest(li)
    tc = _by_id(m)["tc1"]
    assert tc["unit"] == "ms" and tc["scale"] == 1e-3
    assert tc["max"] == pytest.approx(cfg.limits.tc_max_s * 1e3)
    rev0 = m["revision"]
    cfg.limits.tc_max_s = 10.0
    assert build_manifest(li)["revision"] != rev0
    cfg.limits.tc_max_s = Config().limits.tc_max_s
    assert build_manifest(li)["revision"] == rev0


def test_revision_ignores_measured_values(brain):
    cfg, li = brain
    import time
    rev0 = build_manifest(li)["revision"]
    time.sleep(0.2)                           # the poller updates live values
    assert build_manifest(li)["revision"] == rev0


def test_describe_over_the_wire_and_both_status_paths_agree(brain):
    cfg, li = brain
    li.shutdown()                            # the service starts it again
    svc = Hf2Service(li, host="127.0.0.1", cmd_port=15893, pub_port=15894)
    svc.start()
    cli = None
    try:
        cli = Hf2Client(host="127.0.0.1", cmd_port=15893, pub_port=15894)
        m = cli.describe()
        assert m["module"] == "hf2"
        direct = cli._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
    finally:
        if cli is not None:
            cli.shutdown()
        svc.stop()
