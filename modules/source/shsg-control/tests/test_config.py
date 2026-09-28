"""Config defaults and INI save/load round-trip (including the bool fields)."""

from shsg.config import Config, Signal, Limits, Hardware


def test_defaults():
    cfg = Config()
    assert isinstance(cfg.signal, Signal)
    assert isinstance(cfg.limits, Limits)
    assert isinstance(cfg.hardware, Hardware)
    assert cfg.signal.rf_on is False
    # the signalhound service's default ports, and the clean-stop rule
    assert (cfg.hardware.owner_cmd_port, cfg.hardware.owner_pub_port) == (5587, 5588)
    assert cfg.hardware.off_on_shutdown is True


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.signal.frequency_Hz = 2.4e9
    cfg.signal.power_dBm = -13.5
    cfg.signal.rf_on = True
    cfg.limits.power_max_dBm = -12.0
    cfg.hardware.owner_host = "192.168.1.7"
    cfg.hardware.owner_cmd_port = 6001
    cfg.hardware.off_on_shutdown = False
    cfg.hardware.echo_tol_Hz = 5.0

    path = tmp_path / "shsg.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.signal.frequency_Hz == 2.4e9
    assert back.signal.power_dBm == -13.5
    # the important edge case: a bool survives the string round-trip (gotcha #3)
    assert back.signal.rf_on is True
    assert back.hardware.off_on_shutdown is False
    assert back.limits.power_max_dBm == -12.0
    assert back.hardware.owner_host == "192.168.1.7"
    assert back.hardware.owner_cmd_port == 6001
    assert isinstance(back.hardware.owner_cmd_port, int)
    assert back.hardware.echo_tol_Hz == 5.0


def test_bool_false_roundtrip(tmp_path):
    cfg = Config()
    cfg.signal.rf_on = False
    path = tmp_path / "shsg.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).signal.rf_on is False


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"          # safe default
    cfg.ui.theme = "light"
    path = tmp_path / "shsg.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"
