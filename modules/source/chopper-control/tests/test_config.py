"""Config defaults and INI save/load round-trip (including the bool fields),
and the wire path's type casting."""

from chopper.config import Config, Blades, Limits, Settle, Hardware, Sim
from chopper.net.protocol import apply_config_dict, config_to_dict


def test_defaults():
    cfg = Config()
    assert isinstance(cfg.blades, Blades)
    assert isinstance(cfg.limits, Limits)
    assert isinstance(cfg.settle, Settle)
    assert isinstance(cfg.hardware, Hardware)
    assert isinstance(cfg.sim, Sim)
    assert "MC1F10HP" in cfg.blades.owned and "MC1F60" in cfg.blades.owned
    assert cfg.hardware.baud == 115200
    assert cfg.hardware.stop_on_exit is False     # a running chopper is left running


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.blades.owned = "MC1F60"
    cfg.limits.freq_max_Hz = 2500.0
    cfg.settle.hold_s = 2.5
    cfg.hardware.port = "COM9"
    cfg.hardware.baud = 57600
    cfg.hardware.stop_on_exit = True
    cfg.sim.enabled = False
    cfg.sim.frequency_Hz = 333.3

    path = tmp_path / "chopper.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.blades.owned == "MC1F60"
    assert back.limits.freq_max_Hz == 2500.0
    assert back.settle.hold_s == 2.5
    assert back.hardware.port == "COM9"
    assert back.hardware.baud == 57600 and isinstance(back.hardware.baud, int)
    # the important edge cases: bools survive the string round-trip both ways
    assert back.hardware.stop_on_exit is True
    assert back.sim.enabled is False
    assert back.sim.frequency_Hz == 333.3


def test_bool_false_and_true_roundtrip(tmp_path):
    for value in (False, True):
        cfg = Config()
        cfg.hardware.stop_on_exit = value
        path = tmp_path / f"c_{value}.ini"
        cfg.save(str(path))
        assert Config.load(str(path)).hardware.stop_on_exit is value


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"
    cfg.ui.theme = "light"
    path = tmp_path / "chopper.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"


def test_wire_config_is_cast_to_field_types():
    """A client sending "false" or an int where a float belongs must not plant
    a wrong type (bool("false") is True -- gotcha #3)."""
    cfg = Config()
    apply_config_dict(cfg, {"hardware": {"stop_on_exit": "false", "baud": "9600"},
                            "limits": {"freq_max_Hz": 5000},
                            "nonsense": {"x": 1}})
    assert cfg.hardware.stop_on_exit is False
    assert cfg.hardware.baud == 9600
    assert isinstance(cfg.limits.freq_max_Hz, float)
    d = config_to_dict(cfg)
    assert set(d) == {"blades", "limits", "settle", "hardware", "sim", "ui"}
