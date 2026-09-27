"""Config defaults and INI save/load round-trip (including the bool fields)."""

from dssg.config import Config, Signal, Limits, Hardware, Sim, UI
from dssg.net.protocol import apply_config_dict, config_to_dict


def test_defaults_are_safe():
    cfg = Config()
    assert isinstance(cfg.signal, Signal) and isinstance(cfg.limits, Limits)
    assert isinstance(cfg.hardware, Hardware) and isinstance(cfg.sim, Sim)
    assert isinstance(cfg.ui, UI)
    assert not hasattr(cfg.signal, "rf_on"), "RF-on-at-start must not be configurable"
    assert cfg.limits.power_max_dBm <= 10.0          # at or below the calibrated max
    assert cfg.hardware.tcp_port == 10001             # DSI's fixed data port
    assert cfg.hardware.baud == 115200
    # adopt-on-start: nothing that changes the unit is on by default
    assert cfg.hardware.mute_buzzer is False and cfg.hardware.display_off is False


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.signal.frequency_Hz = 2.4e9
    cfg.signal.power_dBm = -3.5
    cfg.signal.reference = "external"
    cfg.limits.power_max_dBm = 8.0
    cfg.hardware.transport = "tcp"
    cfg.hardware.host = "10.0.0.23"
    cfg.hardware.tcp_port = 10002
    cfg.hardware.mute_buzzer = False
    cfg.sim.external_ref_present = True
    cfg.sim.has_phase = False

    path = tmp_path / "dssg.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.signal.frequency_Hz == 2.4e9
    assert back.signal.power_dBm == -3.5
    assert back.signal.reference == "external"
    assert back.limits.power_max_dBm == 8.0
    assert back.hardware.transport == "tcp"
    assert back.hardware.host == "10.0.0.23"
    assert back.hardware.tcp_port == 10002 and isinstance(back.hardware.tcp_port, int)
    # the important edge cases: bools survive the string round-trip both ways
    assert back.hardware.mute_buzzer is False
    assert back.hardware.display_off is False
    assert back.sim.external_ref_present is True
    assert back.sim.has_phase is False


def test_every_group_travels_over_the_wire():
    """gotcha #4: a group missing from config_to_dict silently stays behind."""
    d = config_to_dict(Config())
    assert set(d) == set(Config._GROUPS)


def test_wire_config_parses_text_bools():
    """A hand-written client sending "False" must not switch a bool ON."""
    cfg = Config()
    apply_config_dict(cfg, {"hardware": {"mute_buzzer": "False", "poll_hz": "2.5"},
                            "nonsense": {"x": 1}, "sim": {"unknown_key": 3}})
    assert cfg.hardware.mute_buzzer is False
    assert cfg.hardware.poll_hz == 2.5


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"
    cfg.ui.theme = "light"
    path = tmp_path / "dssg.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"
