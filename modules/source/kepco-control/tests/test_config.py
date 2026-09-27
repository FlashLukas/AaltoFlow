"""Config defaults and INI save/load round-trip (including the bool fields)."""

from dataclasses import fields

from kepco.config import Config


def test_defaults_are_safe():
    cfg = Config()
    assert cfg.output.mode == "current"
    assert cfg.output.current_A == 0.0 and cfg.output.voltage_V == 0.0
    assert cfg.ramp.enabled is True
    assert cfg.limits.voltage_max_V == 20.0 and cfg.limits.current_max_A == 10.0
    assert cfg.safety.shutdown_ramp_s < 8.0      # the launcher kills at 8 s
    assert cfg.hardware.visa == "GPIB0::6::INSTR"


def test_every_group_round_trips(tmp_path):
    cfg = Config()
    cfg.output.mode = "voltage"
    cfg.output.voltage_V = -3.25
    cfg.output.current_limit_A = 0.75
    cfg.ramp.enabled = False                     # the classic bool gotcha (#3)
    cfg.ramp.rate_A_per_s = 0.125
    cfg.limits.current_max_A = 4.0
    cfg.safety.watchdog_s = 2.5
    cfg.acquisition.readings = 7
    cfg.hardware.visa = "GPIB0::11::INSTR"
    cfg.hardware.full_range = False
    cfg.sim.load_L_H = 0.02
    cfg.ui.theme = "light"

    path = tmp_path / "kepco.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    for group in Config._GROUPS:
        a, b = getattr(cfg, group), getattr(back, group)
        for f in fields(a):
            assert getattr(a, f.name) == getattr(b, f.name), (group, f.name)
            assert type(getattr(a, f.name)) is type(getattr(b, f.name)), (group, f.name)
    assert back.ramp.enabled is False
    assert back.hardware.full_range is False


def test_bool_true_round_trip(tmp_path):
    cfg = Config()
    cfg.ramp.enabled = True
    path = tmp_path / "kepco.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ramp.enabled is True


def test_file_is_utf8(tmp_path):
    path = tmp_path / "kepco.ini"
    Config().save(str(path))
    path.read_text(encoding="utf-8")             # must not raise
