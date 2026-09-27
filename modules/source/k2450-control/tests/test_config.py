"""Config defaults and INI save/load round-trip (including the bool fields)."""

from k2450.config import Config, Source, Measure, Limits, Hardware, Sim, _cast


def test_defaults_are_safe():
    cfg = Config()
    assert isinstance(cfg.source, Source) and isinstance(cfg.measure, Measure)
    assert isinstance(cfg.limits, Limits) and isinstance(cfg.hardware, Hardware)
    assert isinstance(cfg.sim, Sim)
    assert cfg.source.function == "voltage"
    assert cfg.source.voltage_V == 0.0
    assert cfg.source.current_limit_A <= 1e-3          # a low compliance by default
    # the 2450's own envelope
    assert cfg.limits.voltage_max_V == 210.0
    assert cfg.limits.current_max_A == 1.05
    assert (cfg.limits.box_voltage_V, cfg.limits.box_current_A) == (21.0, 0.105)


def test_no_output_on_at_start_setting_exists():
    """Deliberate: there is no way to configure the output ON at start-up."""
    for group in ("source", "measure", "hardware"):
        assert not any("output" in f for f in vars(getattr(Config(), group)))


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.source.function = "current"
    cfg.source.current_A = 1.5e-6
    cfg.source.voltage_limit_V = 5.0
    cfg.source.auto_range = False
    cfg.measure.nplc = 0.5
    cfg.measure.four_wire = True
    cfg.acquisition.readings = 12
    cfg.hardware.visa_resource = "TCPIP0::10.0.0.5::inst0::INSTR"
    cfg.hardware.visa_timeout_ms = 8000
    cfg.sim.load = "diode"

    path = tmp_path / "k2450.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.source.function == "current"
    assert back.source.current_A == 1.5e-6
    assert back.source.voltage_limit_V == 5.0
    assert back.source.auto_range is False          # a bool survives the string trip
    assert back.measure.four_wire is True
    assert back.measure.nplc == 0.5
    assert back.acquisition.readings == 12 and isinstance(back.acquisition.readings, int)
    assert back.hardware.visa_resource == "TCPIP0::10.0.0.5::inst0::INSTR"
    assert back.hardware.visa_timeout_ms == 8000
    assert back.sim.load == "diode"


def test_bool_false_roundtrip(tmp_path):
    """bool("False") is True -- the classic INI trap (gotcha #3)."""
    cfg = Config()
    cfg.measure.auto_range = False
    cfg.measure.four_wire = False
    path = tmp_path / "k2450.ini"
    cfg.save(str(path))
    back = Config.load(str(path))
    assert back.measure.auto_range is False
    assert back.measure.four_wire is False
    assert _cast("False", "bool") is False
    assert _cast("on", "bool") is True


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"
    cfg.ui.theme = "light"
    path = tmp_path / "k2450.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"


def test_every_group_is_saved(tmp_path):
    """A group missing from _GROUPS silently does not persist (gotcha #4)."""
    path = tmp_path / "k2450.ini"
    Config().save(str(path))
    text = path.read_text(encoding="utf-8")
    for g in ("source", "measure", "acquisition", "limits", "hardware", "sim", "ui"):
        assert f"[{g}]" in text
