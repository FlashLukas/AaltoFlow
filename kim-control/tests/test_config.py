"""Config INI round-trip (§9): every value survives save->load, incl. bools."""

from kim.config import Config, load_config, save_config


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.motion.rate_x = 1500.0
    cfg.motion.voltage_y = 118.0
    cfg.motion.jog_steps = 250
    cfg.calibration.um_per_step_z = 0.0185
    cfg.limits.max_steps_x = 900_000
    cfg.hardware.serial = "97123456"
    cfg.hardware.ch_z = 4

    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    loaded = load_config(str(path))

    assert loaded.motion.rate_x == 1500.0
    assert loaded.motion.voltage_y == 118.0
    assert loaded.motion.jog_steps == 250
    assert loaded.calibration.um_per_step_z == 0.0185
    assert loaded.limits.max_steps_x == 900_000
    assert loaded.hardware.serial == "97123456"
    assert loaded.hardware.ch_z == 4
    # UI theme persists in the INI
    cfg.ui.theme = "light"
    save_config(cfg, str(path))
    assert load_config(str(path)).ui.theme == "light"
    # types, not just values
    assert isinstance(loaded.hardware.ch_z, int)
    assert isinstance(loaded.motion.jog_steps, int)
    assert isinstance(loaded.motion.rate_x, float)
    assert isinstance(loaded.limits.max_steps_x, int)


def test_bool_field_survives_both_ways(tmp_path):
    """The classic trap: 'False' is a truthy string. Assert both True and False."""
    for value in (True, False):
        cfg = Config()
        cfg.limits.enforce = value
        cfg.hardware.swap_xy = value

        path = tmp_path / f"cfg_{value}.ini"
        save_config(cfg, str(path))
        loaded = load_config(str(path))

        assert loaded.limits.enforce is value
        assert loaded.hardware.swap_xy is value
