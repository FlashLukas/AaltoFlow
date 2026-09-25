"""Config round-trips through the .ini, bools included (bool("False") is True)."""

from mag2dcal.config import Config


def test_defaults_match_the_contract():
    cfg = Config()
    assert cfg.hardware.ao_x == "Dev1/ao0" and cfg.hardware.ao_y == "Dev1/ao1"
    assert cfg.hardware.di_water == "Dev1/port0/line1"
    assert cfg.hardware.do_enable == "Dev1/port0/line0"
    assert cfg.hall.x_mV_per_mT == -13.38466 and cfg.hall.y_offset_V == 2.4973
    assert cfg.limits.field_max_mT == 180.0
    assert (cfg.limits.angle_min_deg, cfg.limits.angle_max_deg) == (-360.0, 360.0)
    assert cfg.control.settle_timeout_s == 30.0
    assert cfg.control.energize_on_start is True
    assert cfg.interlock.water_bypass is False and cfg.interlock.temp_monitor is False
    assert cfg.interlock.max_temp_C == 40.0
    # this module's own groups
    assert cfg.control.freeze_enabled is True          # the whole point
    assert cfg.control.field_step_mT == 2.0            # clMag's undershoot
    assert cfg.control.trim_slew_V_per_s < cfg.control.slew_V_per_s
    assert cfg.stabilizer.enabled is True
    assert cfg.stabilizer.deadband_mT < cfg.control.tolerance_mT
    assert cfg.calibration.directory == "Calibrations"
    assert cfg.calibration.load_newest_on_start is True


def test_hall_conversion_round_trips():
    cfg = Config()
    vx, vy = cfg.hall.mT_to_volts(123.4, -56.7)
    bx, by = cfg.hall.volts_to_mT(vx, vy)
    assert abs(bx - 123.4) < 1e-9 and abs(by + 56.7) < 1e-9
    # the VI's numbers: X has a negative slope, so +B gives a LOWER voltage
    assert vx < cfg.hall.x_offset_V and cfg.hall.mT_to_volts(0, 10)[1] > cfg.hall.y_offset_V


def test_ini_round_trip_including_bools(tmp_path):
    cfg = Config()
    cfg.control.kp_V_per_mT = 0.033
    cfg.control.energize_on_start = False
    cfg.interlock.water_bypass = True
    cfg.interlock.temp_monitor = True
    cfg.hardware.hall_samples = 250
    cfg.hardware.ai_terminal = "RSE"
    cfg.sim.water_ok = False
    cfg.ui.theme = "light"
    cfg.control.freeze_enabled = False
    cfg.stabilizer.enabled = False
    cfg.stabilizer.gain_V_per_mT = 0.02
    cfg.calibration.load_newest_on_start = False
    cfg.calibration.auto_save = False
    cfg.calibration.n_per_leg = 31
    cfg.calibration.directory = r"C:\somewhere\Calibrations"
    path = tmp_path / "mag2dcal.ini"
    cfg.save(str(path))

    back = Config.load(str(path))
    assert back.control.kp_V_per_mT == 0.033
    assert back.control.energize_on_start is False      # the classic bool trap
    assert back.interlock.water_bypass is True
    assert back.interlock.temp_monitor is True
    assert back.sim.water_ok is False
    assert back.hardware.hall_samples == 250 and isinstance(back.hardware.hall_samples, int)
    assert back.hardware.ai_terminal == "RSE"
    assert back.ui.theme == "light"
    # the new groups travel too -- a group forgotten in _GROUPS is silently lost
    assert back.control.freeze_enabled is False        # bool trap again
    assert back.stabilizer.enabled is False and back.stabilizer.gain_V_per_mT == 0.02
    assert back.calibration.load_newest_on_start is False
    assert back.calibration.auto_save is False
    assert back.calibration.n_per_leg == 31 and isinstance(back.calibration.n_per_leg, int)
    assert back.calibration.directory == r"C:\somewhere\Calibrations"


def test_partial_ini_keeps_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[limits]\nfield_max_mT = 100\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.limits.field_max_mT == 100.0
    assert back.control.tolerance_mT == Config().control.tolerance_mT
    assert back.stabilizer.enabled is True            # a whole missing group


def test_every_group_is_listed_in_both_places():
    """A new config group has to be added to _GROUPS AND __post_init__ (docs/
    DEVELOPER_NOTES.md gotcha #4). Forget one and it silently stops travelling."""
    cfg = Config()
    for name in Config._GROUPS:
        assert getattr(cfg, name) is not None, name
    from dataclasses import fields as dataclass_fields
    declared = {f.name for f in dataclass_fields(Config)}
    assert declared == set(Config._GROUPS)
