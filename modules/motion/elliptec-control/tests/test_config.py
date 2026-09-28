"""Config INI round-trip: every value survives save->load, incl. bools (gotcha #3)."""

import pytest

from elliptec.config import (Config, axis_names, get_offsets, load_config,
                             parse_addresses, save_config, set_offsets)
from elliptec.net.protocol import apply_config_dict, config_to_dict


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.axes.addresses = "0,1"
    cfg.axes.names = "HWP,polarizer"
    cfg.motion.velocity_pct = 60
    cfg.limits.max_angle_deg = 180.0
    cfg.offsets.offsets_deg = "12.5,-3"
    cfg.hardware.port = "COM7"
    cfg.sim.max_speed_deg_s = 300.0

    path = tmp_path / "cfg.ini"
    save_config(cfg, str(path))
    back = load_config(str(path))

    assert back.axes.addresses == "0,1"
    assert back.axes.names == "HWP,polarizer"
    assert back.motion.velocity_pct == 60 and isinstance(back.motion.velocity_pct, int)
    assert back.limits.max_angle_deg == 180.0 and isinstance(back.limits.max_angle_deg, float)
    assert back.offsets.offsets_deg == "12.5,-3"
    assert back.hardware.port == "COM7"
    assert back.sim.max_speed_deg_s == 300.0


def test_bool_fields_survive_both_ways(tmp_path):
    """The classic trap: 'False' is a truthy string. Assert both True and False."""
    for value in (True, False):
        cfg = Config()
        cfg.limits.enforce = value
        cfg.motion.home_on_start = value
        path = tmp_path / f"cfg_{value}.ini"
        save_config(cfg, str(path))
        back = load_config(str(path))
        assert back.limits.enforce is value
        assert back.motion.home_on_start is value


def test_bool_string_over_the_wire_is_parsed():
    cfg = Config()
    apply_config_dict(cfg, {"limits": {"enforce": "False"}, "motion": {"home_on_start": True}})
    assert cfg.limits.enforce is False
    assert cfg.motion.home_on_start is True


def test_every_group_travels_over_the_wire():
    cfg = Config()
    d = config_to_dict(cfg)
    assert set(d) == {"axes", "motion", "limits", "offsets", "hardware", "sim", "ui"}
    d["sim"]["start_deg"] = 5.0
    d["axes"]["addresses"] = "3"
    target = Config()
    apply_config_dict(target, d)
    assert target.sim.start_deg == 5.0 and target.axes.addresses == "3"


def test_address_parsing():
    assert parse_addresses("0, 1,a") == ["0", "1", "A"]
    for bad in ("", "G", "10", "1,1"):
        with pytest.raises(ValueError):
            parse_addresses(bad)


def test_names_and_offsets_helpers():
    cfg = Config()
    cfg.axes.addresses = "0,1,2"
    cfg.axes.names = "HWP,,QWP"
    assert axis_names(cfg) == ["HWP", "Axis 1", "QWP"]
    cfg.offsets.offsets_deg = "10, junk"
    assert get_offsets(cfg, 3) == [10.0, 0.0, 0.0]
    set_offsets(cfg, [1.5, 0.0, 359.0])
    assert get_offsets(cfg, 3) == [1.5, 0.0, 359.0]
