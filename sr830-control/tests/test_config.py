"""Config defaults and INI save/load round-trip (bools, ints, floats, labels)."""

from sr830.config import Config, CHOICES


def test_defaults_are_safe_and_match_the_instrument():
    cfg = Config()
    assert cfg.reference.sine_out_V == cfg.limits.sine_min_V == 0.004   # SINE OUT minimum
    assert cfg.hardware.resource == "GPIB0::8::INSTR"                   # factory address 8
    assert cfg.safety.sine_min_on_stop is True and cfg.safety.aux_out_zero_on_stop is True
    assert cfg.limits.freq_max_Hz == 102000.0
    assert cfg.ui.theme == "dark"
    # every enum default is one of its own options
    for (group, name), options in CHOICES.items():
        assert getattr(getattr(cfg, group), name) in options, (group, name)


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.reference.source = "external"
    cfg.reference.harmonic = 2
    cfg.reference.frequency_Hz = 12345.5
    cfg.input.source = "A-B"
    cfg.demod.sensitivity = "500 uV"
    cfg.demod.time_constant = "300 ms"
    cfg.demod.sync_filter = True
    cfg.safety.sine_min_on_stop = False
    cfg.hardware.front_panel_override = False
    cfg.aux_out.out3_V = -1.25
    cfg.acquisition.average_tc = 3.0
    cfg.ui.theme = "light"

    path = tmp_path / "sr830.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.reference.source == "external"
    assert back.reference.harmonic == 2 and isinstance(back.reference.harmonic, int)
    assert back.reference.frequency_Hz == 12345.5
    assert back.input.source == "A-B"
    assert back.demod.sensitivity == "500 uV"
    assert back.demod.time_constant == "300 ms"
    # the bool trap: bool("False") is True, so both directions must survive
    assert back.demod.sync_filter is True
    assert back.safety.sine_min_on_stop is False
    assert back.hardware.front_panel_override is False
    assert back.aux_out.out3_V == -1.25
    assert back.acquisition.average_tc == 3.0
    assert back.ui.theme == "light"


def test_partial_ini_keeps_the_other_defaults(tmp_path):
    path = tmp_path / "part.ini"
    path.write_text("[demod]\nslope = 12 dB/oct\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.demod.slope == "12 dB/oct"
    assert back.demod.time_constant == Config().demod.time_constant
    assert back.reference.sine_out_V == 0.004
