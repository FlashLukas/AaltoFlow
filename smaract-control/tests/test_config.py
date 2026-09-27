"""Config INI round-trip (section 9): every value survives save->load, incl. bools."""

from smaract.config import GROUPS, Config, load_config, save_config
from smaract.net.protocol import apply_config_dict, config_to_dict


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.motion.velocity_mm_s = 3.25
    cfg.motion.hold_time_ms = 1500
    cfg.limits.max_mm = 80.5
    cfg.relative.rel_origin_mm = -12.25
    cfg.hardware.nm_per_count = 10.0
    cfg.hardware.channel = 2
    cfg.hardware.dll_path = r"C:\SmarAct\SCU3DControl.dll"

    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    loaded = load_config(str(path))

    assert loaded.motion.velocity_mm_s == 3.25
    assert loaded.motion.hold_time_ms == 1500
    assert loaded.limits.max_mm == 80.5
    assert loaded.relative.rel_origin_mm == -12.25
    assert loaded.hardware.nm_per_count == 10.0
    assert loaded.hardware.dll_path == r"C:\SmarAct\SCU3DControl.dll"
    # types, not just values
    assert isinstance(loaded.hardware.channel, int) and loaded.hardware.channel == 2
    assert isinstance(loaded.motion.hold_time_ms, int)
    assert isinstance(loaded.motion.velocity_mm_s, float)


def test_bool_fields_survive_both_ways(tmp_path):
    """The classic trap (gotcha #3): 'False' is a truthy string."""
    for value in (True, False):
        cfg = Config()
        cfg.limits.enforce = value
        cfg.motion.reference_on_start = value
        cfg.motion.require_reference = value
        cfg.hardware.invert = value
        path = tmp_path / f"cfg_{value}.ini"
        save_config(cfg, str(path))
        loaded = load_config(str(path))
        assert loaded.limits.enforce is value
        assert loaded.motion.reference_on_start is value
        assert loaded.motion.require_reference is value
        assert loaded.hardware.invert is value


def test_every_group_travels_over_the_wire():
    """gotcha #4: a group missing from config_to_dict never reaches the service."""
    d = config_to_dict(Config())
    assert set(d) == set(GROUPS)
    cfg = Config()
    cfg.limits.max_mm = 50.0
    cfg.relative.rel_origin_mm = 3.0
    target = Config()
    apply_config_dict(target, config_to_dict(cfg))
    assert target.limits.max_mm == 50.0 and target.relative.rel_origin_mm == 3.0


def test_apply_config_dict_casts_strings():
    """A hand-typed set_config with "False" must give False, not True."""
    cfg = Config()
    apply_config_dict(cfg, {"limits": {"enforce": "False", "max_mm": "12.5"},
                            "hardware": {"channel": "1"}, "bogus": {"x": 1}})
    assert cfg.limits.enforce is False
    assert cfg.limits.max_mm == 12.5
    assert cfg.hardware.channel == 1
