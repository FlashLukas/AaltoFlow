"""Config defaults and INI save/load round-trip (including the bool field)."""

from smb.config import Config, Signal, Limits, Hardware


def test_defaults():
    cfg = Config()
    assert isinstance(cfg.signal, Signal)
    assert isinstance(cfg.limits, Limits)
    assert isinstance(cfg.hardware, Hardware)
    assert cfg.signal.rf_on is False
    assert cfg.hardware.smb_visa == "GPIB0::28::INSTR"


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.signal.frequency_Hz = 2.4e9
    cfg.signal.power_dBm = -3.5
    cfg.signal.phase_deg = 12.0
    cfg.signal.rf_on = True
    cfg.limits.power_max_dBm = 20.0
    cfg.hardware.smb_visa = "GPIB0::15::INSTR"
    cfg.hardware.visa_timeout_ms = 8000

    path = tmp_path / "smb.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.signal.frequency_Hz == 2.4e9
    assert back.signal.power_dBm == -3.5
    assert back.signal.phase_deg == 12.0
    # the important edge case: a bool survives the string round-trip
    assert back.signal.rf_on is True
    assert back.limits.power_max_dBm == 20.0
    assert back.hardware.smb_visa == "GPIB0::15::INSTR"
    assert back.hardware.visa_timeout_ms == 8000
    assert isinstance(back.hardware.visa_timeout_ms, int)


def test_bool_false_roundtrip(tmp_path):
    cfg = Config()
    cfg.signal.rf_on = False
    path = tmp_path / "smb.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).signal.rf_on is False


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"          # safe default
    cfg.ui.theme = "light"
    path = tmp_path / "smb.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"
