"""Config defaults and INI save/load round-trip (including the bool fields)."""

from superk.config import (Config, Startup, Limits, Filters, Hardware, N_LINES,
                           floats, ints, names, join)


def test_defaults_are_safe():
    cfg = Config()
    assert isinstance(cfg.startup, Startup)
    assert isinstance(cfg.limits, Limits)
    assert isinstance(cfg.filters, Filters)
    assert isinstance(cfg.hardware, Hardware)
    # there is no way to ask for emission at start in the config at all
    assert not any("emission" in f for f in vars(cfg.startup))
    assert cfg.limits.power_max_pct < 100
    assert cfg.hardware.emission_off_on_start is True
    assert cfg.hardware.watchdog_s > 0
    assert names(cfg.filters.names) == ["VIS-nIR", "nIR2", "IR"]


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.startup.power_pct = 22.5
    cfg.startup.filter = "IR"
    cfg.startup.wavelengths_nm = "1300,1500"
    cfg.limits.power_max_pct = 70.0
    cfg.filters.min_nm = "450,800,1100"
    cfg.hardware.port = "COM9"
    cfg.hardware.rf_addr = 17
    cfg.hardware.poll_hz = 2.0

    path = tmp_path / "superk.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.startup.power_pct == 22.5
    assert back.startup.filter == "IR"
    assert back.startup.wavelengths_nm == "1300,1500"
    assert back.limits.power_max_pct == 70.0
    assert back.filters.min_nm == "450,800,1100"
    assert back.hardware.port == "COM9"
    assert back.hardware.rf_addr == 17 and isinstance(back.hardware.rf_addr, int)
    assert back.hardware.poll_hz == 2.0


def test_bools_survive_the_string_roundtrip(tmp_path):
    """bool("False") is True -- the classic trap (gotcha #3)."""
    for value in (True, False):
        cfg = Config()
        cfg.hardware.autodetect = value
        cfg.hardware.emission_off_on_start = value
        path = tmp_path / f"b{value}.ini"
        cfg.save(str(path))
        back = Config.load(str(path))
        assert back.hardware.autodetect is value
        assert back.hardware.emission_off_on_start is value


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"
    cfg.ui.theme = "light"
    path = tmp_path / "superk.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"


def test_list_helpers_pad_and_tolerate_junk():
    assert floats("650, 700", N_LINES) == [650.0, 700.0] + [0.0] * (N_LINES - 2)
    assert floats("1,x,3") == [1.0, 0.0, 3.0]
    assert ints("1,2,4") == [1, 2, 4]
    assert join([650.0, 700.5]) == "650,700.5"
