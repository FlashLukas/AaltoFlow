"""Config defaults and INI save/load round-trip (including the bool field)."""

from dsamp.config import Amp, Config, Hardware, Limits


def test_defaults_are_safe():
    cfg = Config()
    assert isinstance(cfg.amp, Amp)
    assert isinstance(cfg.limits, Limits)
    assert isinstance(cfg.hardware, Hardware)
    # the safety ceiling starts well below the device maximum
    assert cfg.limits.gain_max_dB < cfg.hardware.gain_max_dB
    # the state is ADOPTED at start: no start-up gain, no "on at start-up"
    assert not hasattr(cfg.amp, "startup_gain_dB")
    assert not hasattr(cfg.amp, "amp_on")
    assert cfg.hardware.baud == 115200
    assert cfg.limits.input_max_dBm <= 10.0


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.amp.frequency_Hz = 2.4e9
    cfg.amp.input_dBm = -12.5
    cfg.limits.gain_max_dB = 20.0
    cfg.hardware.port = "COM9"
    cfg.hardware.baud = 57600
    cfg.hardware.gain_step_dB = 0.25
    cfg.hardware.buttons_on_exit = False

    path = tmp_path / "dsamp.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.amp.frequency_Hz == 2.4e9
    assert back.amp.input_dBm == -12.5
    assert back.limits.gain_max_dB == 20.0
    assert back.hardware.port == "COM9"
    assert back.hardware.baud == 57600 and isinstance(back.hardware.baud, int)
    assert back.hardware.gain_step_dB == 0.25
    # the classic edge case: bool("False") is True, so the loader must PARSE it
    assert back.hardware.buttons_on_exit is False


def test_bool_true_roundtrip(tmp_path):
    cfg = Config()
    cfg.hardware.buttons_on_exit = True
    path = tmp_path / "dsamp.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).hardware.buttons_on_exit is True


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"
    cfg.ui.theme = "light"
    path = tmp_path / "dsamp.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"


def test_ini_is_utf8_and_ascii_safe(tmp_path):
    path = tmp_path / "dsamp.ini"
    Config().save(str(path))
    path.read_bytes().decode("ascii")      # nothing exotic in the file we write


def test_config_over_the_wire_is_cast_to_the_field_types():
    """A hand-typed set_config sends strings; they must arrive as numbers/bools."""
    from dsamp.net.protocol import apply_config_dict
    cfg = Config()
    apply_config_dict(cfg, {"limits": {"gain_max_dB": "15"},
                            "hardware": {"buttons_on_exit": "False", "baud": "9600"}})
    assert cfg.limits.gain_max_dB == 15.0 and isinstance(cfg.limits.gain_max_dB, float)
    assert cfg.hardware.buttons_on_exit is False        # not bool("False")
    assert cfg.hardware.baud == 9600


def test_bad_config_value_is_refused_and_nothing_is_written():
    """All-or-nothing: one bad value leaves the WHOLE config untouched, so a
    half-applied set_config can never leave a string where a number belongs."""
    import pytest
    from dsamp.net.protocol import apply_config_dict
    cfg = Config()
    with pytest.raises(ValueError):
        apply_config_dict(cfg, {"limits": {"gain_min_dB": 2.0, "gain_max_dB": "lots"}})
    assert cfg.limits.gain_min_dB == 0.0
    assert cfg.limits.gain_max_dB == 10.0
    with pytest.raises(ValueError):
        apply_config_dict(cfg, {"limits": {"gain_max_dB": float("nan")}})


def test_old_ini_with_startup_gain_still_loads(tmp_path):
    """startup_gain_dB was removed (the gain is adopted at start); an .ini that
    still carries it must load, the key simply ignored."""
    path = tmp_path / "old.ini"
    path.write_text("[amp]" + chr(10) + "startup_gain_dB = 7.5" + chr(10)
                    + "input_dBm = -30" + chr(10), encoding="utf-8")
    back = Config.load(str(path))
    assert back.amp.input_dBm == -30.0
    assert not hasattr(back.amp, "startup_gain_dB")
