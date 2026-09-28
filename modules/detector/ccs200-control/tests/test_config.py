"""Config round-trips through .ini, including the bool and str fields."""

from ccs200.config import Config, _cast


def test_ini_round_trip(tmp_path):
    cfg = Config()
    cfg.scan.integration_time_s = 0.0375
    cfg.scan.averages = 12
    cfg.scan.dark_subtract = True
    cfg.scan.continuous = False
    cfg.analysis.window_min_nm = 540.0
    cfg.sim.light_on = False
    cfg.hardware.resource = "USB0::0x1313::0x8089::M00000000::RAW"
    cfg.hardware.calibration = "user"
    cfg.ui.theme = "light"
    path = tmp_path / "ccs200.ini"
    cfg.save(str(path))
    back = Config.load(str(path))
    assert back.scan.integration_time_s == 0.0375
    assert back.scan.averages == 12 and isinstance(back.scan.averages, int)
    # the bools: bool("False") is True, so these would all be True if cast naively
    assert back.scan.dark_subtract is True and back.scan.continuous is False
    assert back.sim.light_on is False
    assert back.analysis.window_min_nm == 540.0
    assert back.hardware.resource.endswith("::RAW") and back.hardware.calibration == "user"
    assert back.ui.theme == "light"


def test_missing_groups_and_keys_keep_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[scan]\naverages = 3\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.scan.averages == 3
    assert cfg.scan.integration_time_s == 0.01 and cfg.sim.light_on is True


def test_cast_bools():
    for text in ("False", "false", "0", "no", "off", ""):
        assert _cast(text, "bool") is False
    for text in ("True", "yes", "1", "on"):
        assert _cast(text, "bool") is True
    assert _cast("5e-3", "float") == 0.005 and _cast("7.0", "int") == 7
