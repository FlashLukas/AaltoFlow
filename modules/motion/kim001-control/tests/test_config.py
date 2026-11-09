"""Config INI round-trip (§9): every value survives save->load, incl. bools."""

from kim001.config import Config, load_config, save_config


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.motion.rate_z = 1500.0
    cfg.motion.voltage_z = 118.0
    cfg.motion.jog_steps = 250
    cfg.calibration.um_per_step_z = 0.0185
    cfg.limits.max_steps_z = 900_000
    cfg.hardware.serial = "97123456"
    cfg.hardware.ch_z = 4
    cfg.hardware.serial_prefix = "74"

    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    loaded = load_config(str(path))

    assert loaded.motion.rate_z == 1500.0
    assert loaded.motion.voltage_z == 118.0
    assert loaded.motion.jog_steps == 250
    assert loaded.calibration.um_per_step_z == 0.0185
    assert loaded.limits.max_steps_z == 900_000
    assert loaded.hardware.serial == "97123456"
    assert loaded.hardware.ch_z == 4
    assert loaded.hardware.serial_prefix == "74"     # a str, not cast to int
    # UI theme persists in the INI
    cfg.ui.theme = "light"
    save_config(cfg, str(path))
    assert load_config(str(path)).ui.theme == "light"
    # types, not just values
    assert isinstance(loaded.hardware.ch_z, int)
    assert isinstance(loaded.motion.jog_steps, int)
    assert isinstance(loaded.motion.rate_z, float)
    assert isinstance(loaded.limits.max_steps_z, int)


def test_bool_field_survives_both_ways(tmp_path):
    """The classic trap: 'False' is a truthy string. Assert both True and False."""
    for value in (True, False):
        cfg = Config()
        cfg.limits.enforce = value
        cfg.limits.leash_enabled = value

        path = tmp_path / f"cfg_{value}.ini"
        save_config(cfg, str(path))
        loaded = load_config(str(path))

        assert loaded.limits.enforce is value
        assert loaded.limits.leash_enabled is value


def test_defaults_describe_a_kim001_with_one_pia13():
    """One channel (Z on channel 1), PIA13 travel +/-13 mm at 20 nm around the
    datum, X / Y 0..0, kim's safe slow/small drive defaults."""
    from kim001.config import (FITTED_AXES, axis_effective_limits, check_fitted,
                               NotFitted)
    import pytest

    cfg = Config()
    assert FITTED_AXES == (2,)
    assert (cfg.hardware.ch_x, cfg.hardware.ch_y, cfg.hardware.ch_z) == (0, 0, 1)
    assert cfg.hardware.serial == "" and cfg.hardware.serial_prefix   # a guess, # VERIFY
    assert cfg.calibration.um_per_step_z == 0.02
    assert (cfg.limits.min_steps_z, cfg.limits.max_steps_z) == (-650_000, 650_000)
    assert 650_000 * cfg.calibration.um_per_step_z == pytest.approx(13_000.0)  # 13 mm
    assert (cfg.motion.voltage_z, cfg.motion.rate_z, cfg.motion.acc_z) == (85.0, 300.0, 5000.0)
    for axis in (0, 1):
        assert axis_effective_limits(cfg, axis) == (0, 0)
        cfg.limits.leash_enabled = True
        assert axis_effective_limits(cfg, axis) == (0, 0)     # leash or not
        cfg.limits.leash_enabled = False
        with pytest.raises(NotFitted, match="not fitted on a KIM001"):
            check_fitted(axis)
    check_fitted(2)
