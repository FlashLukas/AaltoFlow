"""The declared TYPE of every detector is how scan-core STORES it (2026-10-04,
developer notes 4b). An enum value outside its options is stored as "not
measured"; an int outside its declared min/max STOPS a scan. So: the mode enum
lists every mode the supply can be in (from the code: config.MODES, which
open() and set_mode enforce), and the acquisition counter declares only the
range it can really have (>= 0, no upper bound)."""

import pytest

from kepco.config import MODES, Config
from kepco.net.describe import build_manifest
from kepco.sim_system import build_sim_system


@pytest.fixture
def supply():
    s, _ = build_sim_system(Config(), seed=0)
    s.start(poll=False)
    yield s
    s.shutdown()


def _by_id(s):
    return {p["id"]: p for p in build_manifest(s)["parameters"]}


def test_mode_enum_covers_every_mode(supply):
    p = _by_id(supply)
    assert p["mode"]["type"] == "enum"
    assert set(p["mode"]["options"]) == set(MODES)
    assert supply.status().mode in p["mode"]["options"]


def test_acq_id_is_a_non_negative_counter_without_an_upper_bound(supply):
    d = _by_id(supply)["acq_id"]
    assert d["type"] == "int" and d["min"] == 0 and "max" not in d
    assert supply.status().acq_id >= 0
    first = supply.acquire()
    assert first >= 1 and supply.acquire() == first + 1


def test_flags_are_bools(supply):
    p = _by_id(supply)
    for flag in ("output", "ramping", "acquiring", "at_limit", "output_state",
                 "connected", "ramp_enabled"):
        assert p[flag]["type"] == "bool", flag
