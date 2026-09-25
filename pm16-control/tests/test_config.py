"""Config round-trips through the .ini, bools included (bool("False") is True)."""

from pm16.config import Config


def test_defaults():
    cfg = Config()
    assert cfg.sensor.auto_range is True
    assert cfg.hardware.push_on_start is False
    assert cfg.acquisition.readings >= 1


def test_ini_round_trip(tmp_path):
    cfg = Config()
    cfg.sensor.wavelength_nm = 1064.0
    cfg.sensor.auto_range = False
    cfg.sensor.range_W = 0.0174
    cfg.acquisition.readings = 12
    cfg.hardware.resource = "USB0::0x1313::0x807B::000000000::INSTR"
    cfg.hardware.push_on_start = True
    cfg.ui.theme = "light"
    path = tmp_path / "pm16.ini"
    cfg.save(str(path))

    back = Config.load(str(path))
    assert back.sensor.wavelength_nm == 1064.0
    assert back.sensor.auto_range is False          # the classic bool trap
    assert back.sensor.range_W == 0.0174
    assert back.acquisition.readings == 12
    assert back.hardware.resource.endswith("000000000::INSTR")
    assert back.hardware.push_on_start is True
    assert back.ui.theme == "light"


def test_partial_ini_keeps_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[sensor]\nwavelength_nm = 633\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.sensor.wavelength_nm == 633.0
    assert back.limits.wavelength_max_nm == Config().limits.wavelength_max_nm
