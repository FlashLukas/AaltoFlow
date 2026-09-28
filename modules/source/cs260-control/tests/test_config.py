"""Config: defaults, the .ini round trip (bools included -- gotcha #3), and the
small text parsers."""

import pytest

from cs260.config import Config, _cast, parse_filter_bands, parse_labels


def test_defaults_are_the_safe_configuration():
    cfg = Config()
    assert cfg.hardware.visa == "GPIB0::4::INSTR"        # manual: factory address 4
    assert cfg.accessories.filter_wheel is False          # never command absent hardware
    assert cfg.accessories.dual_port is False
    # start changes nothing (Lukas's rule 2026-09-27): the option is gone
    assert not hasattr(cfg.shutter, "close_on_start")
    assert cfg.gratings.count == 2
    assert cfg.gratings.of(1)[0] == 1200


def test_ini_round_trip_including_bools(tmp_path):
    cfg = Config()
    cfg.gratings.count = 3
    cfg.gratings.g2_label = "BLAZE1000"
    cfg.gratings.g1_max_nm = 1234.5
    cfg.accessories.filter_wheel = True
    cfg.accessories.dual_port = True
    cfg.shutter.close_on_shutdown = False        # a False that must survive
    cfg.hardware.visa = "GPIB0::7::INSTR"
    cfg.ui.theme = "light"
    path = tmp_path / "cs260.ini"
    cfg.save(str(path))
    back = Config.load(str(path))
    assert back.gratings.count == 3
    assert back.gratings.g2_label == "BLAZE1000"
    assert back.gratings.g1_max_nm == 1234.5
    assert back.accessories.filter_wheel is True
    assert back.accessories.dual_port is True
    assert back.shutter.close_on_shutdown is False
    assert back.hardware.visa == "GPIB0::7::INSTR"
    assert back.ui.theme == "light"


def test_bool_cast_parses_text():
    assert _cast("False", "bool") is False
    assert _cast("false", "bool") is False
    assert _cast("0", "bool") is False
    assert _cast("True", "bool") is True
    assert _cast("on", "bool") is True
    assert _cast("3", "int") == 3


def test_missing_section_keeps_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[limits]\nwavelength_max_nm = 900\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.limits.wavelength_max_nm == 900.0
    assert back.gratings.count == 2


def test_parsers():
    assert parse_labels("a,,c", 4) == ["a", "2", "c", "4"]
    assert parse_filter_bands("1:0-420, 2:420-750") == [(1, 0.0, 420.0), (2, 420.0, 750.0)]
    with pytest.raises(ValueError):
        parse_filter_bands("x:1-2")


def test_old_ini_with_close_on_start_still_loads(tmp_path):
    """close_on_start was removed; an .ini written before must still load."""
    path = tmp_path / "old.ini"
    path.write_text("[shutter]\nclose_on_start = True\nclose_on_shutdown = False\n",
                    encoding="utf-8")
    back = Config.load(str(path))
    assert back.shutter.close_on_shutdown is False
    assert not hasattr(back.shutter, "close_on_start")
