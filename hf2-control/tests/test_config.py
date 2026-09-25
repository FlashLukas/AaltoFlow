"""Config defaults and INI save/load round-trip (bools, ints, strings, two channels)."""

from hf2.config import Config


def test_defaults_give_two_independent_channels():
    cfg = Config()
    assert (cfg.ch1.demod, cfg.ch1.signal_input, cfg.ch1.oscillator) == (0, 0, 0)
    assert (cfg.ch2.demod, cfg.ch2.signal_input, cfg.ch2.oscillator) == (3, 1, 1)
    assert cfg.hardware.server_port == 8005        # the HF2 data server, not 8004
    assert cfg.hardware.api_level == 1
    assert cfg.ui.theme == "dark"


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.ch1.time_constant_s = 0.3
    cfg.ch1.order = 2
    cfg.ch1.reference = "internal"
    cfg.ch2.frequency_Hz = 12345.5
    cfg.ch2.input_ac = True
    cfg.ch2.input_50ohm = False
    cfg.acquisition.average_tc = 3.0
    cfg.hardware.device_id = "dev1234"
    cfg.ui.theme = "light"

    path = tmp_path / "hf2.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.ch1.time_constant_s == 0.3
    assert back.ch1.order == 2 and isinstance(back.ch1.order, int)
    assert back.ch1.reference == "internal"
    assert back.ch2.frequency_Hz == 12345.5
    assert back.ch2.input_ac is True               # the bool trap: bool("False") is True
    assert back.ch2.input_50ohm is False
    assert back.acquisition.average_tc == 3.0
    assert back.hardware.device_id == "dev1234"
    assert back.ui.theme == "light"


def test_partial_ini_keeps_channel_specific_defaults(tmp_path):
    """An .ini with only [ch1] must not reset ch2 to ch1's defaults."""
    path = tmp_path / "part.ini"
    path.write_text("[ch1]\norder = 3\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.ch1.order == 3
    assert (back.ch2.demod, back.ch2.signal_input) == (3, 1)
