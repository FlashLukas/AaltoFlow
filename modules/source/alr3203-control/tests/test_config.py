"""Config defaults and INI save/load round-trip (including the bool fields)."""

from dataclasses import fields

import pytest

from alr3203.config import Config, dio_index


def test_defaults_are_safe():
    cfg = Config()
    assert cfg.output.mode == "current"
    assert cfg.output.current_A == 0.0 and cfg.output.voltage_V == 0.0
    assert cfg.ramp.enabled is True
    # the ALR3203's ratings, both signs (the negative half through the relay)
    assert cfg.limits.voltage_max_V == 32.0 and cfg.limits.current_max_A == 6.0
    assert cfg.limits.voltage_min_V == -32.0 and cfg.limits.current_min_A == -6.0
    assert cfg.limits.power_max_W == 96.0
    # Lukas: the limit channel sits at the full range
    assert cfg.output.voltage_limit_V == 32.0 and cfg.output.current_limit_A == 6.0
    assert cfg.safety.shutdown_ramp_s < 8.0      # the launcher kills at 8 s
    assert cfg.hardware.com == ""                # never guessed: the PC's ini says
    assert cfg.polarity.driver == "sim"
    assert cfg.polarity.zero_current_A == 0.005  # Lukas: 5 mA
    assert cfg.polarity.line == "p0.0"


def test_every_group_round_trips(tmp_path):
    cfg = Config()
    cfg.output.mode = "voltage"
    cfg.output.voltage_V = -3.25
    cfg.output.current_limit_A = 0.75
    cfg.ramp.enabled = False                     # the classic bool gotcha (#3)
    cfg.ramp.rate_A_per_s = 0.125
    cfg.limits.current_max_A = 4.0
    cfg.polarity.driver = "usb6001"
    cfg.polarity.inverted_level = "low"
    cfg.polarity.deadband_A = 0.02
    cfg.safety.watchdog_s = 2.5
    cfg.acquisition.readings = 7
    cfg.hardware.com = "COM7"
    cfg.sim.load_L_H = 0.02
    cfg.sim.found_relay_inverted = True
    cfg.ui.theme = "light"

    path = tmp_path / "alr3203.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    for group in Config._GROUPS:
        a, b = getattr(cfg, group), getattr(back, group)
        for f in fields(a):
            assert getattr(a, f.name) == getattr(b, f.name), (group, f.name)
            assert type(getattr(a, f.name)) is type(getattr(b, f.name)), (group, f.name)
    assert back.ramp.enabled is False
    assert back.sim.found_relay_inverted is True


def test_bool_true_round_trip(tmp_path):
    cfg = Config()
    cfg.ramp.enabled = True
    path = tmp_path / "alr3203.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ramp.enabled is True


def test_file_is_utf8(tmp_path):
    path = tmp_path / "alr3203.ini"
    Config().save(str(path))
    path.read_text(encoding="utf-8")             # must not raise


@pytest.mark.parametrize("spelling,index", [("p0.0", 0), ("P0.7", 7), ("p1.2", 10),
                                            ("Dev1/port0/line0", 0), ("port2/line0", 12)])
def test_dio_line_names(spelling, index):
    assert dio_index(spelling) == index


def test_bad_dio_line_is_refused():
    with pytest.raises(ValueError):
        dio_index("p3.0")
