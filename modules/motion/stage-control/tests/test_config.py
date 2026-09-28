"""Config INI round-trip (§9): every value survives save->load, incl. bools."""

from stage.config import Config, load_config, save_config


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.motion.vel_x = 3.25
    cfg.limits.max_x = 12.5
    cfg.offsets.off_z = -0.75
    cfg.transform.m01 = -1.0
    cfg.hardware.serial = "70123456"
    cfg.hardware.ch_z = 3

    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    loaded = load_config(str(path))

    assert loaded.motion.vel_x == 3.25
    assert loaded.limits.max_x == 12.5
    assert loaded.offsets.off_z == -0.75
    assert loaded.transform.m01 == -1.0
    assert loaded.hardware.serial == "70123456"
    assert loaded.hardware.ch_z == 3
    # types, not just values
    assert isinstance(loaded.hardware.ch_z, int)
    assert isinstance(loaded.motion.vel_x, float)


def test_bool_field_survives_both_ways(tmp_path):
    """The classic trap: 'False' is a truthy string. Assert both True and False."""
    for value in (True, False):
        cfg = Config()
        cfg.limits.enforce = value
        cfg.motion.home_on_start = value
        cfg.hardware.swap_xy = value

        path = tmp_path / f"cfg_{value}.ini"
        save_config(cfg, str(path))
        loaded = load_config(str(path))

        assert loaded.limits.enforce is value
        assert loaded.motion.home_on_start is value
        assert loaded.hardware.swap_xy is value
