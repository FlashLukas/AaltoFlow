"""Config INI round-trip (§9): every value survives save->load, incl. bools."""

from piezo.config import Config, load_config, save_config


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.motion.vel_x = 123.5
    cfg.motion.ramp_hz = 40.0
    cfg.motion.ramp_mode = "hardware"
    cfg.limits.travel_max_cl = 150.0
    cfg.limits.max_velocity = 999.0
    cfg.relative.rel_y = -3.25
    cfg.hardware.port = "COM7"
    cfg.hardware.baud = 57600
    cfg.hardware.ch_y = 2
    cfg.ui.theme = "light"

    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    loaded = load_config(str(path))

    assert loaded.ui.theme == "light"

    assert loaded.motion.vel_x == 123.5
    assert loaded.motion.ramp_hz == 40.0
    assert loaded.motion.ramp_mode == "hardware"
    assert loaded.limits.travel_max_cl == 150.0
    assert loaded.limits.max_velocity == 999.0
    assert loaded.relative.rel_y == -3.25
    assert loaded.hardware.port == "COM7"
    assert loaded.hardware.ch_y == 2
    # types, not just values
    assert isinstance(loaded.hardware.baud, int)
    assert isinstance(loaded.motion.vel_x, float)


def test_bad_ramp_mode_is_sanitised(tmp_path):
    cfg = Config()
    cfg.motion.ramp_mode = "nonsense"
    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    loaded = load_config(str(path))
    assert loaded.motion.ramp_mode == "software"


def test_bad_theme_is_sanitised(tmp_path):
    cfg = Config()
    cfg.ui.theme = "neon"
    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    loaded = load_config(str(path))
    assert loaded.ui.theme == "dark"


def test_bool_field_survives_both_ways(tmp_path):
    """The classic trap: 'False' is a truthy string. Assert both True and False."""
    for value in (True, False):
        cfg = Config()
        cfg.limits.enforce = value
        cfg.motion.closed_loop_x = value
        cfg.hardware.swap_xy = value

        path = tmp_path / f"cfg_{value}.ini"
        save_config(cfg, str(path))
        loaded = load_config(str(path))

        assert loaded.limits.enforce is value
        assert loaded.motion.closed_loop_x is value
        assert loaded.hardware.swap_xy is value
