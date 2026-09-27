"""Config defaults and INI save/load round-trip (including the bool field and
the freq_command template, whose braces must survive configparser)."""

from dsphase.config import Config, Signal, Limits, Device, Hardware, _cast


def test_defaults_are_safe():
    cfg = Config()
    assert isinstance(cfg.signal, Signal)
    assert isinstance(cfg.limits, Limits)
    assert isinstance(cfg.device, Device)
    assert isinstance(cfg.hardware, Hardware)
    assert cfg.signal.output_on is False            # output OFF at start
    assert cfg.device.phase_step_deg == 0.5         # PS6000L datasheet
    assert cfg.device.att_step_dB == 0.25
    assert cfg.device.freq_command == ""            # not in the V3 command list
    assert cfg.hardware.baud == 115200              # command list V3


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.signal.phase_deg = 12.5
    cfg.signal.attenuation_dB = 3.75
    cfg.signal.frequency_MHz = 5800.0
    cfg.signal.output_on = True
    cfg.limits.att_min_dB = 6.0
    cfg.device.phase_step_deg = 5.625
    cfg.device.freq_command = "FREQ {mhz:.3f}MHZ"
    cfg.hardware.port = "COM9"
    cfg.hardware.baud = 57600
    cfg.ui.theme = "light"

    path = tmp_path / "dsphase.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.signal.phase_deg == 12.5
    assert back.signal.attenuation_dB == 3.75
    assert back.signal.frequency_MHz == 5800.0
    assert back.signal.output_on is True            # the bool edge case
    assert back.limits.att_min_dB == 6.0
    assert back.device.phase_step_deg == 5.625
    assert back.device.freq_command == "FREQ {mhz:.3f}MHZ"
    assert back.hardware.port == "COM9"
    assert back.hardware.baud == 57600 and isinstance(back.hardware.baud, int)
    assert back.ui.theme == "light"


def test_bool_false_roundtrip(tmp_path):
    cfg = Config()
    cfg.signal.output_on = False
    path = tmp_path / "dsphase.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).signal.output_on is False


def test_cast_parses_bool_text():
    # bool("False") is True in Python -- the cast must parse (gotcha #3)
    assert _cast("False", "bool") is False
    assert _cast("off", "bool") is False
    assert _cast("True", "bool") is True
    assert _cast("7", "int") == 7


def test_missing_group_keeps_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[signal]\nphase_deg = 45\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.signal.phase_deg == 45.0
    assert back.device.phase_step_deg == 0.5


def test_wire_values_get_the_field_type():
    # set_config from a script / the console may carry strings; a string step
    # size would crash the rounding and bool("off") is True (gotcha #3).
    from dsphase.net.protocol import apply_config_dict
    cfg = Config()
    apply_config_dict(cfg, {"device": {"phase_step_deg": "5.625"},
                            "signal": {"output_on": "off"},
                            "hardware": {"baud": "9600", "poll_hz": 2},
                            "limits": {"att_max_dB": 20}})
    assert cfg.device.phase_step_deg == 5.625
    assert cfg.signal.output_on is False
    assert cfg.hardware.baud == 9600
    assert isinstance(cfg.hardware.poll_hz, float)
    assert isinstance(cfg.limits.att_max_dB, float)
    apply_config_dict(cfg, {"signal": {"output_on": True}})
    assert cfg.signal.output_on is True
