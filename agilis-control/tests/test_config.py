"""Config INI round-trip: every value survives save->load, incl. bools."""

from agilis.config import (
    Config,
    axis_effective_limits,
    axis_um_per_step,
    hardware_axis,
    load_config,
    save_config,
)


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.motion.amp_fwd_x = 23
    cfg.motion.amp_bwd_y = 41
    cfg.motion.jog_timeout_s = 2.5
    cfg.calibration.um_per_step_y_bwd = 0.0412
    cfg.calibration.amp_y_bwd = 41
    cfg.limits.max_steps_x = 90_000
    cfg.hardware.port = "COM7"
    cfg.hardware.channel = 3
    cfg.ui.theme = "light"

    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    loaded = load_config(str(path))

    assert loaded.motion.amp_fwd_x == 23 and isinstance(loaded.motion.amp_fwd_x, int)
    assert loaded.motion.amp_bwd_y == 41
    assert loaded.motion.jog_timeout_s == 2.5
    assert loaded.calibration.um_per_step_y_bwd == 0.0412
    assert loaded.calibration.amp_y_bwd == 41
    assert loaded.limits.max_steps_x == 90_000
    assert loaded.hardware.port == "COM7"
    assert loaded.hardware.channel == 3 and isinstance(loaded.hardware.channel, int)
    assert loaded.hardware.baud == 921600
    assert loaded.ui.theme == "light"


def test_bool_fields_survive_both_ways(tmp_path):
    """The classic trap: 'False' is a truthy string. Assert both True and False."""
    for value in (True, False):
        cfg = Config()
        cfg.limits.enforce = value
        cfg.limits.leash_enabled = value
        cfg.hardware.swap_xy = value
        cfg.hardware.local_on_close = value
        path = tmp_path / f"cfg_{value}.ini"
        save_config(cfg, str(path))
        loaded = load_config(str(path))
        assert loaded.limits.enforce is value
        assert loaded.limits.leash_enabled is value
        assert loaded.hardware.swap_xy is value
        assert loaded.hardware.local_on_close is value


def test_directional_step_size_and_leash_accessors():
    cfg = Config()
    assert axis_um_per_step(cfg, 0, -1) == axis_um_per_step(cfg, 0, +1)  # bwd 0 = same
    cfg.calibration.um_per_step_x_bwd = 0.03
    assert axis_um_per_step(cfg, 0, -1) == 0.03
    assert axis_um_per_step(cfg, 0, 0) == (0.05 + 0.03) / 2
    assert axis_effective_limits(cfg, 1) == (-300_000, 300_000)
    cfg.limits.leash_enabled = True
    cfg.limits.leash_steps = 500
    assert axis_effective_limits(cfg, 1) == (-500, 500)


def test_swap_xy_maps_controller_axes():
    cfg = Config()
    assert (hardware_axis(cfg, 0), hardware_axis(cfg, 1)) == (1, 2)
    cfg.hardware.swap_xy = True
    assert (hardware_axis(cfg, 0), hardware_axis(cfg, 1)) == (2, 1)
