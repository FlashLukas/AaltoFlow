"""Config defaults and the INI round trip (bool, int, str fields)."""

from scope.config import Config, _cast


def test_defaults():
    cfg = Config()
    assert cfg.channel_1 is not cfg.channel_2
    assert cfg.channel("ch2") is cfg.channel_2
    assert cfg.channel_1.phys_unit == "V" and cfg.channel_1.phys_scale == 1.0
    assert cfg.filter.lowpass_Hz == 0 and cfg.filter.highpass_Hz == 0   # off by default
    assert not hasattr(cfg, "analysis")           # no loop analysis in a scope


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.channel_1.phys_scale = 2500.0
    cfg.channel_1.phys_unit = "A"
    cfg.channel_2.enabled = False
    cfg.acquisition.points = 512
    cfg.acquisition.keep_raw = True
    cfg.filter.lowpass_Hz = 1500.0
    cfg.sim.ch2_phase_deg = 90.0
    cfg.hardware.visa = "GPIB0::18::INSTR"
    path = tmp_path / "scope.ini"
    cfg.save(str(path))
    back = Config.load(str(path))
    assert back.channel_1.phys_scale == 2500.0 and back.channel_1.phys_unit == "A"
    assert back.channel_2.enabled is False
    assert back.acquisition.points == 512 and isinstance(back.acquisition.points, int)
    assert back.acquisition.keep_raw is True
    assert back.filter.lowpass_Hz == 1500.0
    assert back.sim.ch2_phase_deg == 90.0 and back.hardware.visa == "GPIB0::18::INSTR"


def test_an_old_ini_with_loop_settings_still_loads(tmp_path):
    """scope.ini files written before the loop analysis was removed carry an
    [analysis] group and MOKE sim fields: ignored, not an error."""
    path = tmp_path / "old.ini"
    path.write_text("[analysis]\nloop_x = ch1\nsat_fraction = 0.8\n"
                    "[sim]\nscene = moke\nhc_mT = 12\nfrequency_Hz = 50\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.sim.frequency_Hz == 50.0


def test_cast_parses_text():
    assert _cast("False", "bool") is False         # gotcha #3
    assert _cast("on", "bool") is True
    assert _cast("512.0", "int") == 512
