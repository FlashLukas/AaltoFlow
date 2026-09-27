"""Config round-trips through the .ini, bools included (bool("False") is True)."""

from ls455.config import Config


def test_defaults():
    cfg = Config()
    assert cfg.meter.mode == "dc"
    assert cfg.meter.auto_range is True
    assert cfg.meter.relative is False
    assert cfg.hardware.push_on_start is False
    assert cfg.hardware.resource == "GPIB0::12::INSTR"      # the 455's factory address
    assert cfg.acquisition.readings >= 1


def test_ini_round_trip(tmp_path):
    cfg = Config()
    cfg.meter.mode = "rms"
    cfg.meter.dc_digits = 5
    cfg.meter.rms_band = "narrow"
    cfg.meter.auto_range = False
    cfg.meter.range_mT = 35.0
    cfg.meter.display_unit = "A/m"
    cfg.meter.relative = True
    cfg.meter.rel_setpoint_mT = -12.5
    cfg.acquisition.readings = 12
    cfg.acquisition.settle_time_constants = 5.0
    cfg.hardware.resource = "ASRL3::INSTR"
    cfg.hardware.baud_rate = 19200
    cfg.hardware.push_on_start = True
    cfg.ui.theme = "light"
    path = tmp_path / "ls455.ini"
    cfg.save(str(path))

    back = Config.load(str(path))
    assert back.meter.mode == "rms"
    assert back.meter.dc_digits == 5
    assert back.meter.rms_band == "narrow"
    assert back.meter.auto_range is False            # the classic bool trap
    assert back.meter.relative is True
    assert back.meter.range_mT == 35.0
    assert back.meter.display_unit == "A/m"
    assert back.meter.rel_setpoint_mT == -12.5
    assert back.acquisition.readings == 12
    assert back.acquisition.settle_time_constants == 5.0
    assert back.hardware.resource == "ASRL3::INSTR"
    assert back.hardware.baud_rate == 19200
    assert back.hardware.push_on_start is True
    assert back.ui.theme == "light"


def test_false_bools_survive(tmp_path):
    cfg = Config()
    cfg.meter.auto_range = False
    cfg.hardware.push_on_start = False
    path = tmp_path / "f.ini"
    cfg.save(str(path))
    back = Config.load(str(path))
    assert back.meter.auto_range is False and back.hardware.push_on_start is False


def test_partial_ini_keeps_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[meter]\nrange_mT = 3500\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.meter.range_mT == 3500.0
    assert back.limits.range_max_mT == Config().limits.range_max_mT
