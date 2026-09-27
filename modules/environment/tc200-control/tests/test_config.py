"""Config defaults and INI save/load round-trip (including the bool fields)."""

from tc200.config import Config, Device, Hardware, Limits, Temperature, _cast
from tc200.net.protocol import apply_config_dict, config_to_dict


def test_defaults_are_the_safe_ones():
    cfg = Config()
    assert isinstance(cfg.temperature, Temperature) and isinstance(cfg.device, Device)
    assert isinstance(cfg.limits, Limits) and isinstance(cfg.hardware, Hardware)
    # the rig has a PT100; the service adopts rather than pushes; the heater is
    # switched OFF when the service stops (the safer choice)
    assert cfg.hardware.expected_sensor == "ptc100"
    assert cfg.device.sensor == "ptc100"
    # start only READS the box: the push-at-start option is gone (2026-09-27)
    assert not hasattr(cfg.hardware, "push_on_start")
    assert cfg.hardware.disable_on_shutdown is True
    # manual 6.3.1: 115200 8N1
    assert cfg.hardware.baud == 115200
    # the TC200 cannot set below 20 C, and our ceiling sits under the box's 200
    assert cfg.limits.temperature_min_C == 20.0
    assert cfg.limits.temperature_max_C <= 200.0


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.temperature.tolerance_C = 0.35
    cfg.device.p_gain = 90
    cfg.device.sensor = "ptc1000"
    cfg.limits.temperature_max_C = 150.0
    cfg.hardware.port = "COM9"
    cfg.hardware.stat_base = 10
    cfg.hardware.disable_on_shutdown = False
    cfg.ui.theme = "light"

    path = tmp_path / "tc200.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.temperature.tolerance_C == 0.35
    assert back.device.p_gain == 90 and isinstance(back.device.p_gain, int)
    assert back.device.sensor == "ptc1000"
    assert back.limits.temperature_max_C == 150.0
    assert back.hardware.port == "COM9"
    assert back.hardware.stat_base == 10 and isinstance(back.hardware.stat_base, int)
    # the important edge case: bools survive the string round-trip, BOTH ways
    assert back.hardware.disable_on_shutdown is False
    cfg2 = Config()                                  # and a True survives too
    cfg2.save(str(tmp_path / "t.ini"))
    assert Config.load(str(tmp_path / "t.ini")).hardware.disable_on_shutdown is True
    assert back.ui.theme == "light"


def test_bool_cast_parses_the_text():
    assert _cast("False", "bool") is False
    assert _cast("false", "bool") is False
    assert _cast("0", "bool") is False
    assert _cast("True", "bool") is True
    assert _cast("on", "bool") is True


def test_missing_file_gives_defaults(tmp_path):
    back = Config.load(str(tmp_path / "nope.ini"))
    assert back.hardware.port == Hardware().port


def test_every_group_travels_over_the_wire():
    """gotcha #4: a group missing from config_to_dict silently does not travel."""
    d = config_to_dict(Config())
    assert set(d) == set(Config._GROUPS)
    cfg = Config()
    d["device"]["p_gain"] = 77.0                  # JSON has one number type
    d["hardware"]["disable_on_shutdown"] = "false"
    apply_config_dict(cfg, d)
    assert cfg.device.p_gain == 77 and isinstance(cfg.device.p_gain, int)
    assert cfg.hardware.disable_on_shutdown is False


def test_an_old_ini_with_push_on_start_still_loads(tmp_path):
    """The option was removed (start only reads the box); a file written by the
    old version must load, with the stale key ignored."""
    path = tmp_path / "old.ini"
    path.write_text("[hardware]\nport = COM7\npush_on_start = True\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.hardware.port == "COM7"
    assert not hasattr(cfg.hardware, "push_on_start")
