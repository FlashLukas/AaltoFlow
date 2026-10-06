"""Config defaults and INI save/load round-trip (including bool and int fields)."""

from afg.config import Config, Channel, Limits, Coupling, Hardware, _cast, WAVEFORMS


def test_defaults():
    cfg = Config()
    assert isinstance(cfg.channel_1, Channel) and isinstance(cfg.channel_2, Channel)
    assert cfg.channel_1 is not cfg.channel_2          # two groups, not one shared
    assert cfg.limits_1 is not cfg.limits_2
    assert isinstance(cfg.limits_1, Limits) and isinstance(cfg.coupling, Coupling)
    assert isinstance(cfg.hardware, Hardware)
    # there is no "output on at start" setting at all
    assert not hasattr(cfg.channel_1, "output")
    assert cfg.coupling.ch2_follows_ch1 is False
    assert cfg.channel("ch2") is cfg.channel_2 and cfg.limits("ch2") is cfg.limits_2
    assert cfg.channel_1.waveform in WAVEFORMS
    # Lukas 2026-10-06: "full range" -- the lab limits do not narrow the AFG1062
    # (its widest numbers are into high-Z: 20 Vpp, 10 V peak, 60 MHz)
    from afg.backends.tek_afg import afg1062_envelope
    widest = afg1062_envelope("sine", None)
    for lim in (cfg.limits_1, cfg.limits_2):
        assert lim.amplitude_max_Vpp >= widest["amp_max_Vpp"]
        assert lim.peak_max_V >= widest["peak_max_V"]
        assert lim.freq_max_Hz >= widest["freq_max_Hz"]


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.channel_1.waveform = "ramp"
    cfg.channel_1.frequency_Hz = 30.0
    cfg.channel_2.phase_deg = -90.0
    cfg.limits_1.peak_max_V = 1.5
    cfg.coupling.ch2_follows_ch1 = True
    cfg.coupling.phase_offset_deg = 45.0
    cfg.hardware.visa = "USB0::0x0699::0x0353::C1::INSTR"
    cfg.hardware.timeout_ms = 5000
    cfg.hardware.phase_unit = "deg"

    path = tmp_path / "afg.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.channel_1.waveform == "ramp" and back.channel_1.frequency_Hz == 30.0
    assert back.channel_2.phase_deg == -90.0
    assert back.channel_2.amplitude_Vpp == Channel().amplitude_Vpp   # untouched = default
    assert back.limits_1.peak_max_V == 1.5 and back.limits_2.peak_max_V == Limits().peak_max_V
    assert back.coupling.ch2_follows_ch1 is True and back.coupling.phase_offset_deg == 45.0
    assert back.hardware.visa.endswith("C1::INSTR")
    assert back.hardware.timeout_ms == 5000 and isinstance(back.hardware.timeout_ms, int)
    assert back.hardware.phase_unit == "deg"


def test_bool_cast_parses_text():
    # gotcha #3: bool("False") is True -- the cast must PARSE the text
    assert _cast("False", "bool") is False
    assert _cast("true", "bool") is True
    assert _cast("0", "bool") is False
    assert _cast("3000.0", "int") == 3000
