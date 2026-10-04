"""The declared TYPE of every detector is how scan-core STORES it (2026-10-04,
developer notes 4b). An enum value outside its options is stored as "not
measured"; an int outside its declared min/max STOPS a scan. So the source
function enum lists every function the brain can hold (the backend's FUNCS:
the real backend raises on any other reply, the brain falls back into FUNCS),
and the acquisition counter promises only what it keeps (>= 0, no max)."""

from k2450.backends.base import FUNCS
from k2450.config import Config
from k2450.net.describe import build_manifest
from k2450.sim_system import build_sim_system


def _smu():
    smu, _ = build_sim_system(Config(), realtime=False, seed=0)
    smu.start(poll=False)
    return smu


def _by_id(smu):
    return {p["id"]: p for p in build_manifest(smu)["parameters"]}


def test_source_function_enum_covers_every_function():
    smu = _smu()
    try:
        assert set(_by_id(smu)["source_function"]["options"]) == set(FUNCS)
        for fn in FUNCS:
            smu.set_source_function(fn)
            assert smu.status().source_function in _by_id(smu)["source_function"]["options"]
    finally:
        smu.shutdown()


def test_acq_id_is_a_non_negative_counter_without_an_upper_bound():
    smu = _smu()
    try:
        d = _by_id(smu)["acq_id"]
        assert d["type"] == "int" and d["min"] == 0 and "max" not in d
        assert smu.status().acq_id == 0
        smu.set_output(True)
        assert smu.acquire() == 1
    finally:
        smu.shutdown()


def test_flags_are_bools():
    smu = _smu()
    try:
        p = _by_id(smu)
        for flag in ("output", "settled", "tripped", "sample_tripped", "acquiring",
                     "connected", "four_wire", "source_auto_range", "measure_auto_range"):
            assert p[flag]["type"] == "bool", flag
    finally:
        smu.shutdown()
