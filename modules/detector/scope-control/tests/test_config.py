"""Config defaults and the INI round trip (bool, int, str fields)."""

from scope.config import Config, _cast


def test_defaults():
    cfg = Config()
    assert cfg.channel_1 is not cfg.channel_2
    assert cfg.channel("ch2") is cfg.channel_2
    assert cfg.channel_1.phys_label == "Field" and cfg.channel_2.phys_label == "Intensity"
    assert cfg.filter.lowpass_Hz == 0 and cfg.filter.highpass_Hz == 0   # off by default
    assert cfg.analysis.loop_x == "ch1" and cfg.analysis.loop_y == "ch2"


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.channel_1.phys_scale = 2500.0
    cfg.channel_1.phys_unit = "mT"
    cfg.channel_2.enabled = False
    cfg.acquisition.points = 512
    cfg.acquisition.keep_raw = True
    cfg.filter.lowpass_Hz = 1500.0
    cfg.analysis.subtract_background = False
    cfg.sim.scene = "bench"
    cfg.hardware.visa = "GPIB0::18::INSTR"
    path = tmp_path / "scope.ini"
    cfg.save(str(path))
    back = Config.load(str(path))
    assert back.channel_1.phys_scale == 2500.0 and back.channel_1.phys_unit == "mT"
    assert back.channel_2.enabled is False
    assert back.acquisition.points == 512 and isinstance(back.acquisition.points, int)
    assert back.acquisition.keep_raw is True
    assert back.filter.lowpass_Hz == 1500.0
    assert back.analysis.subtract_background is False
    assert back.sim.scene == "bench" and back.hardware.visa == "GPIB0::18::INSTR"


def test_cast_parses_text():
    assert _cast("False", "bool") is False         # gotcha #3
    assert _cast("on", "bool") is True
    assert _cast("512.0", "int") == 512
