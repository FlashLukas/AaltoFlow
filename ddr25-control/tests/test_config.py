"""Config INI round-trip (section 9): every value survives save->load, incl. bools."""

from ddr25.config import Config, _cast, load_config, save_config, wrap_policy
from ddr25.net.protocol import apply_config_dict, config_to_dict


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.motion.velocity = 45.5
    cfg.motion.wrap = "shortest"
    cfg.limits.max_deg = 400.0
    cfg.frame.zero_deg = -12.25
    cfg.hardware.serial = "28999999"
    cfg.hardware.poll_hz = 33.0

    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    loaded = load_config(str(path))

    assert loaded.motion.velocity == 45.5
    assert loaded.motion.wrap == "shortest"
    assert loaded.limits.max_deg == 400.0
    assert loaded.frame.zero_deg == -12.25
    assert loaded.hardware.serial == "28999999"
    assert isinstance(loaded.hardware.poll_hz, float)


def test_bool_fields_survive_both_ways(tmp_path):
    """The classic trap (gotcha #3): 'False' is a truthy string."""
    for value in (True, False):
        cfg = Config()
        cfg.limits.enforce = value
        cfg.motion.require_home = value
        path = tmp_path / f"cfg_{value}.ini"
        save_config(cfg, str(path))
        loaded = load_config(str(path))
        assert loaded.limits.enforce is value
        assert loaded.motion.require_home is value


def test_cast_parses_bool_text():
    assert _cast("False", "bool") is False
    assert _cast("true", "bool") is True
    assert _cast("0", "bool") is False
    assert _cast(False, "bool") is False


def test_wire_config_casts_values():
    """A JSON "false" string over set_config must not become True."""
    cfg = Config()
    apply_config_dict(cfg, {"motion": {"require_home": "false", "velocity": "12"},
                            "nonsense": {"x": 1}, "limits": {"no_such_key": 3}})
    assert cfg.motion.require_home is False
    assert cfg.motion.velocity == 12.0
    d = config_to_dict(cfg)
    assert set(d) == {"motion", "limits", "frame", "hardware", "ui"}


def test_unknown_wrap_falls_back_to_literal():
    cfg = Config()
    cfg.motion.wrap = "sideways"
    assert wrap_policy(cfg) == "literal"
