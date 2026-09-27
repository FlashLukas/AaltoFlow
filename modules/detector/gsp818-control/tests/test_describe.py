"""The describe manifest tells the truth: live limits, settle policies that
point at real status keys, array detectors with an acquire block, actions a
scan may run with a wait block."""

import json

import pytest

from gsp818.analyzer import SpectrumAnalyzer
from gsp818.config import Config
from gsp818.net.describe import build_manifest, read_path
from gsp818.net.protocol import status_to_dict
from gsp818.sim_system import build_sim_system


@pytest.fixture
def sa():
    cfg = Config()
    cfg.acquisition.continuous = False
    a, _ = build_sim_system(cfg, realtime=False, seed=1)
    a.start(run=False)
    a.step()
    yield a
    a.shutdown()


def _by_id(m):
    return {p["id"]: p for p in m["parameters"]}


def test_manifest_is_json_and_names_the_module(sa):
    m = build_manifest(sa)
    json.dumps(m)                                        # no NaN, no numpy
    assert m["module"] == "gsp818" and "GSP-818" in m["label"]
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids))


def test_every_read_path_and_settle_key_exists_in_status(sa):
    st = status_to_dict(sa.status())
    st["sample"] = {"peak_Hz": 1, "peak_dBm": 1, "floor_dBm": 1, "overload": False}
    for p in build_manifest(sa)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None or p["read_path"][0] in st, p["id"]
        settle = p.get("settle")
        if settle and "key" in settle:
            assert settle["key"] in st, (p["id"], settle)


def test_limits_are_live_and_revision_moves(sa):
    m0 = build_manifest(sa)
    p = _by_id(m0)
    assert p["stop"]["max"] == 1800.0 and p["start"]["min"] == pytest.approx(9e3 / 1e6)
    assert p["tg_level"]["min"] == -30.0 and p["tg_level"]["max"] == 0.0
    assert p["rbw"]["max"] == 3000.0 and p["rbw"]["scale"] == 1e3
    sa.set_stop(1e9)
    m1 = build_manifest(sa)
    assert _by_id(m1)["start"]["max"] == pytest.approx((1e9 - 100.0) / 1e6)
    assert m1["revision"] != m0["revision"]
    sa.set_rbw(30e3)                                     # a value, not a limit: same revision
    assert build_manifest(sa)["revision"] == m1["revision"]


def test_array_detectors_and_their_acquire_block(sa):
    p = _by_id(build_manifest(sa))
    for id, key, q in (("power", "power_dBm", "power"), ("norm", "norm_dB", "norm")):
        d = p[id]
        assert d["type"] == "array" and d["dtype"] == "float" and d["shape"] == ["freq"]
        assert d["read"] == {"verb": "get_trace", "key": key,
                             "args": {"which": "sample", "quantity": q}}
        assert d["dims"][0]["coord_verb"] == "get_frequencies"
        assert d["acquire"]["target_key"] == "acq_id"
    # scalars share the SAME acquire group: one sweep feeds them all
    groups = {p[i]["acquire"]["group"] for i in ("power", "norm", "peak_freq", "peak_level",
                                                 "noise_floor", "overload")}
    assert groups == {"sweep"}


def test_actions_and_danger(sa):
    p = _by_id(build_manifest(sa))
    assert p["take_reference"]["wait"]["target_key"] == "acq_id"
    assert p["clear_reference"]["wait"]["ready"]["policy"] == "immediate"
    assert "wait" not in p["acquire"]
    assert p["tg_on"].get("danger") is True
    assert p["tg_on"]["set"] == {"verb": "set_tg", "arg": "on"}


def test_bench_only_in_simulation(sa):
    assert "dut" in _by_id(build_manifest(sa))

    class Real:
        simulated = False
    ids = _by_id(build_manifest(SpectrumAnalyzer(Real(), Config())))
    assert "dut" not in ids and "dut_center_Hz" not in ids and "power" in ids
