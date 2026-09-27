"""Config defaults and INI save/load round-trip (including the bool fields)."""

from hp8648.config import Config, Signal, Limits, Hardware, _cast


def test_defaults():
    cfg = Config()
    assert isinstance(cfg.signal, Signal)
    assert isinstance(cfg.limits, Limits)
    assert isinstance(cfg.hardware, Hardware)
    # the 8648's factory HP-IB address
    assert cfg.hardware.visa_resource == "GPIB0::19::INSTR"
    assert (cfg.limits.freq_min_Hz, cfg.limits.freq_max_Hz) == (9e3, 4e9)
    assert cfg.limits.power_min_dBm == -136.0
    # there is deliberately no "RF on at start-up" switch
    assert not hasattr(cfg.signal, "rf_on")
    assert not hasattr(cfg.signal, "phase_deg")


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.signal.frequency_Hz = 2.4e9
    cfg.signal.power_dBm = -3.5
    cfg.limits.power_max_dBm = 5.0
    cfg.limits.enforce_spec_ceiling = False
    cfg.hardware.visa_resource = "GPIB0::7::INSTR"
    cfg.hardware.visa_timeout_ms = 8000
    cfg.hardware.option_1ea = True
    cfg.hardware.reset_on_open = False
    cfg.hardware.switch_settle_s = 0.25

    path = tmp_path / "hp8648.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.signal.frequency_Hz == 2.4e9
    assert back.signal.power_dBm == -3.5
    assert back.limits.power_max_dBm == 5.0
    assert back.hardware.visa_resource == "GPIB0::7::INSTR"
    assert back.hardware.visa_timeout_ms == 8000
    assert isinstance(back.hardware.visa_timeout_ms, int)
    assert back.hardware.switch_settle_s == 0.25
    # the important edge case: bools survive the string round-trip (gotcha #3)
    assert back.limits.enforce_spec_ceiling is False
    assert back.hardware.option_1ea is True
    assert back.hardware.reset_on_open is False


def test_bool_defaults_roundtrip(tmp_path):
    cfg = Config()
    path = tmp_path / "hp8648.ini"
    cfg.save(str(path))
    back = Config.load(str(path))
    assert back.limits.enforce_spec_ceiling is True
    assert back.hardware.option_1ea is False
    assert back.hardware.reset_on_open is True


def test_cast_parses_bool_text():
    assert _cast("False", "bool") is False
    assert _cast("off", "bool") is False
    assert _cast("True", "bool") is True
    assert _cast("7", "int") == 7


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"
    cfg.ui.theme = "light"
    path = tmp_path / "hp8648.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"


def test_ini_is_utf8(tmp_path):
    """Written and read as UTF-8 (gotcha #27), so a non-ASCII value survives."""
    cfg = Config()
    cfg.hardware.visa_resource = "GPIB0::19::INSTR  # bench °"
    path = tmp_path / "hp8648.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).hardware.visa_resource.endswith("°")
