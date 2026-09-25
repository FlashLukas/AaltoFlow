"""Config defaults and INI save/load round-trip (including the bool field)."""

from ppms.config import Config, Field, Hardware, Limits, Temperature


def test_defaults_follow_the_old_program():
    cfg = Config()
    assert isinstance(cfg.field, Field) and isinstance(cfg.temperature, Temperature)
    assert isinstance(cfg.limits, Limits) and isinstance(cfg.hardware, Hardware)
    # QDInstrument_ControlField.vi: 22 mT/s linear, 20 K/min fast settle,
    # reached within 0.1 mT / 0.5 K
    assert cfg.field.rate_mT_per_s == 22.0 and cfg.field.approach == "linear"
    assert cfg.temperature.rate_K_per_min == 20.0
    assert cfg.temperature.approach == "fast_settle"
    assert cfg.field.tolerance_mT == 0.1 and cfg.temperature.tolerance_K == 0.5
    assert cfg.hardware.flavor == "DYNACOOL"
    assert cfg.hardware.scaffolding is False


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.field.rate_mT_per_s = 5.5
    cfg.field.approach = "oscillate"
    cfg.temperature.tolerance_K = 0.05
    cfg.limits.field_max_mT = 14000.0
    cfg.hardware.mpv_port = 5123
    cfg.hardware.scaffolding = True

    path = tmp_path / "ppms.ini"
    cfg.save(str(path))
    back = Config.load(str(path))

    assert back.field.rate_mT_per_s == 5.5
    assert back.field.approach == "oscillate"
    assert back.temperature.tolerance_K == 0.05
    assert back.limits.field_max_mT == 14000.0
    assert back.hardware.mpv_port == 5123 and isinstance(back.hardware.mpv_port, int)
    # the important edge case: a bool survives the string round-trip
    assert back.hardware.scaffolding is True


def test_bool_false_roundtrip(tmp_path):
    cfg = Config()
    cfg.hardware.scaffolding = False
    path = tmp_path / "ppms.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).hardware.scaffolding is False


def test_ui_theme_default_and_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"          # safe default
    cfg.ui.theme = "light"
    path = tmp_path / "ppms.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).ui.theme == "light"
