"""The declared TYPE of every detector is how scan-core STORES it (2026-10-04,
developer notes 4b). An enum value outside its options is stored as "not
measured", so each enum must cover every value status can show:
  * `mode` comes from the box's cycle bit: "normal" or "cycle", both forced
    here through the brain's own attribute;
  * `sensor` is always one of config.SENSORS: the serial parser raises on any
    other reply and set_sensor refuses others.
"""

from tc200.config import SENSORS, Config
from tc200.net.describe import build_manifest
from tc200.sim_system import build_sim_system


def _brain(start=True):
    built = build_sim_system(Config())
    brain = built[0] if isinstance(built, tuple) else built
    if start:
        brain.start()
    return brain


def test_mode_and_sensor_enums_cover_every_value_status_can_show():
    # not started: no poll thread that could overwrite _cycle under the test
    brain = _brain(start=False)
    try:
        p = {d["id"]: d for d in build_manifest(brain)["parameters"]}
        assert p["mode"]["type"] == "enum"
        seen = set()
        for cycle in (False, True):
            brain._cycle = cycle          # what the poll thread sets from the box
            seen.add(brain.status().mode)
        brain._cycle = False
        assert seen == set(p["mode"]["options"])
        assert p["sensor"]["type"] == "enum"
        assert set(p["sensor"]["options"]) == set(SENSORS)
        assert brain.status().sensor in SENSORS
    finally:
        brain.shutdown()


def test_flags_are_bools_and_no_indicator_promises_an_int_range():
    brain = _brain()
    try:
        p = {d["id"]: d for d in build_manifest(brain)["parameters"]}
        for flag in ("temperature_stable", "sensor_ok", "sensor_alarm",
                     "tmax_alarm", "connected", "enabled"):
            assert p[flag]["type"] == "bool", flag
        assert not [d["id"] for d in p.values()
                    if d["kind"] == "indicator" and d["type"] == "int"]
    finally:
        brain.shutdown()
