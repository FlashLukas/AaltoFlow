"""Config defaults and INI save/load round-trip (including the bool field)."""

from windfreak.config import Config, Channel, Reference, Limits, Hardware, _cast


def test_defaults_are_safe_and_match_the_datasheet():
    cfg = Config()
    assert isinstance(cfg.channel_a, Channel) and isinstance(cfg.channel_b, Channel)
    assert cfg.channel_a is not cfg.channel_b          # two groups, not one shared
    assert isinstance(cfg.reference, Reference)
    assert isinstance(cfg.limits, Limits) and isinstance(cfg.hardware, Hardware)
    # there is no "RF on at start" setting at all
    assert not hasattr(cfg.channel_a, "rf_on")
    assert cfg.limits.freq_min_Hz == 10e6 and cfg.limits.freq_max_Hz == 24e9
    assert cfg.limits.power_max_dBm <= 20.0
    assert cfg.hardware.pll_off_when_rf_off is False
    assert cfg.channel("b") is cfg.channel_b


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.channel_a.frequency_Hz = 2.4e9
    cfg.channel_b.power_dBm = -3.5
    cfg.channel_b.phase_deg = 90.0
    cfg.reference.source = "external"
    cfg.reference.ext_MHz = 100.0
    cfg.limits.power_max_dBm = 10.0
    cfg.hardware.port = "COM9"
    cfg.hardware.pll_off_when_rf_off = True
    cfg.hardware.phase_command = "absolute"

    path = tmp_path / "windfreak.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.channel_a.frequency_Hz == 2.4e9
    assert back.channel_b.power_dBm == -3.5
    assert back.channel_b.phase_deg == 90.0
    assert back.channel_a.power_dBm == Channel().power_dBm   # untouched stays default
    assert back.reference.source == "external"
    assert back.reference.ext_MHz == 100.0
    assert back.limits.power_max_dBm == 10.0
    assert back.hardware.port == "COM9"
    # the important edge case: a bool survives the string round-trip
    assert back.hardware.pll_off_when_rf_off is True
    assert back.hardware.phase_command == "absolute"


def test_bool_false_roundtrip(tmp_path):
    cfg = Config()
    cfg.hardware.pll_off_when_rf_off = False
    path = tmp_path / "windfreak.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).hardware.pll_off_when_rf_off is False


def test_cast_parses_bool_text():
    assert _cast("False", "bool") is False      # bool("False") would be True
    assert _cast("on", "bool") is True
    assert _cast("3", "int") == 3 and _cast("2.5", "float") == 2.5


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"
    cfg.ui.theme = "light"
    path = tmp_path / "windfreak.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"


def test_config_text_over_the_wire_is_parsed_not_truthy():
    """set_config from a console or a hand-written JSON may carry TEXT: "false"
    must stay False (gotcha #3) and "15" must become a float."""
    from windfreak.net.protocol import apply_config_dict
    cfg = Config()
    cfg.hardware.pll_off_when_rf_off = True
    apply_config_dict(cfg, {"hardware": {"pll_off_when_rf_off": "false",
                                         "port": "COM9"},
                            "limits": {"power_max_dBm": "15", "freq_min_Hz": 2e7},
                            "nonsense": {"x": 1}, "limits_extra": 3})
    assert cfg.hardware.pll_off_when_rf_off is False
    assert cfg.hardware.port == "COM9"
    assert cfg.limits.power_max_dBm == 15.0 and isinstance(cfg.limits.power_max_dBm, float)
    assert cfg.limits.freq_min_Hz == 2e7
