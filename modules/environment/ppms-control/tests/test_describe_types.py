"""The declared TYPE of every detector is how scan-core STORES it (2026-10-04,
developer notes 4b). An enum value outside its options is stored as "not
measured", so the approach enums must cover every approach status can show:
the cryostat only ever holds a name from config's tuples (MultiVu's answer is
adopted only when it is one of them, set_config falls back to the first, the
setters refuse others). MultiVu's own status texts (magnet / temperature /
chamber) are open-ended -- MultiPyVu returns whatever MultiVu says -- so they
stay `string`, never an enum that would lose an unexpected text."""

from ppms.config import FIELD_APPROACHES, TEMPERATURE_APPROACHES, Config
from ppms.net.describe import build_manifest
from ppms.sim_system import build_sim_system


def _brain():
    built = build_sim_system(Config())
    brain = built[0] if isinstance(built, tuple) else built
    brain.start()
    return brain


def test_approach_enums_cover_every_value_status_can_show():
    brain = _brain()
    try:
        p = {d["id"]: d for d in build_manifest(brain)["parameters"]}
        assert p["field_approach"]["type"] == "enum"
        assert set(p["field_approach"]["options"]) == set(FIELD_APPROACHES)
        assert p["temperature_approach"]["type"] == "enum"
        assert set(p["temperature_approach"]["options"]) == set(TEMPERATURE_APPROACHES)
        # a set_config with a name the cryostat does not know falls back to a
        # known one -- the status never shows an unlisted approach
        brain.cfg.field.approach = "nonsense"
        brain.cfg.temperature.approach = "nonsense"
        brain.apply_config()
        st = brain.status()
        assert st.field_approach in FIELD_APPROACHES
        assert st.temperature_approach in TEMPERATURE_APPROACHES
    finally:
        brain.shutdown()


def test_status_texts_stay_strings_and_flags_are_bools():
    brain = _brain()
    try:
        p = {d["id"]: d for d in build_manifest(brain)["parameters"]}
        for sid in ("field_status", "temperature_status", "chamber", "idn", "hw_error"):
            assert p[sid]["type"] == "string", sid
        for flag in ("field_stable", "temperature_stable", "connected"):
            assert p[flag]["type"] == "bool", flag
        assert not [d["id"] for d in p.values()
                    if d["kind"] == "indicator" and d["type"] == "int"]
    finally:
        brain.shutdown()
