"""Config defaults and INI save/load round-trip (bools, ints, strings, floats)."""

from sr7230.config import Config


def test_defaults_are_safe():
    cfg = Config()
    assert cfg.reference.amplitude_V == 0.0          # OSC OUT never starts driving
    assert cfg.hardware.osc_zero_on_start and cfg.hardware.osc_off_on_shutdown
    assert cfg.hardware.port == 50000                # the socket with status bytes
    assert cfg.hardware.host == ""                   # no made-up address
    assert cfg.signal.sensitivity_index == 24        # 100 mV: nothing overloads
    assert cfg.ui.theme == "dark"


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.reference.source = "ext_analog"
    cfg.reference.frequency_Hz = 12345.5
    cfg.reference.harmonic = 2
    cfg.signal.input = "A-B"
    cfg.signal.ac_coupled = False
    cfg.signal.fet = True
    cfg.signal.sensitivity_index = 18
    cfg.filter.time_constant_s = 0.3
    cfg.filter.fast_mode = True
    cfg.filter.slope_db = 6
    cfg.acquisition.average_tc = 3.0
    cfg.hardware.host = "10.0.0.7"
    cfg.hardware.option_250kHz = True
    cfg.hardware.osc_off_on_shutdown = False
    cfg.ui.theme = "light"

    path = tmp_path / "sr7230.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.reference.source == "ext_analog"
    assert back.reference.frequency_Hz == 12345.5
    assert back.reference.harmonic == 2 and isinstance(back.reference.harmonic, int)
    assert back.signal.input == "A-B"
    # the bool trap: bool("False") is True, so each must come back as written
    assert back.signal.ac_coupled is False
    assert back.signal.fet is True
    assert back.filter.fast_mode is True
    assert back.hardware.option_250kHz is True
    assert back.hardware.osc_off_on_shutdown is False
    assert back.hardware.osc_zero_on_start is True
    assert back.signal.sensitivity_index == 18
    assert back.filter.time_constant_s == 0.3
    assert back.filter.slope_db == 6
    assert back.acquisition.average_tc == 3.0
    assert back.hardware.host == "10.0.0.7"
    assert back.ui.theme == "light"


def test_partial_ini_keeps_defaults(tmp_path):
    path = tmp_path / "part.ini"
    path.write_text("[filter]\nslope_db = 24\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.filter.slope_db == 24
    assert back.signal.sensitivity_index == 24 and back.reference.amplitude_V == 0.0
