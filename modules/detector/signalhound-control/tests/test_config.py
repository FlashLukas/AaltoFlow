"""Config round-trips through .ini, including the bool and str fields."""

from signalhound.config import Config


def test_round_trip(tmp_path):
    cfg = Config()
    cfg.sweep.center_Hz = 2.45e9
    cfg.sweep.rbw_Hz = 3000.0
    cfg.sweep.reject = False                    # bool("False") is True -- must survive
    cfg.sweep.detector = "peak"
    cfg.tracking.on = True
    cfg.tracking.points = 801
    cfg.tracking.passive_device = False
    cfg.acquisition.continuous = False
    cfg.scene.dut_inserted = False
    cfg.scene.dut_order = 5
    cfg.hardware.model = "SA124B"
    cfg.hardware.serial = 12345678
    cfg.hardware.dll_path = r"C:\Program Files\Signal Hound\sa_api.dll"
    cfg.hardware.attach_tg = False
    path = tmp_path / "signalhound.ini"
    cfg.save(str(path))

    back = Config.load(str(path))
    assert back.sweep.center_Hz == 2.45e9 and back.sweep.rbw_Hz == 3000.0
    assert back.sweep.reject is False and back.sweep.detector == "peak"
    assert back.tracking.on is True and back.tracking.points == 801
    assert isinstance(back.tracking.points, int)
    assert back.tracking.passive_device is False and back.tracking.high_dynamic_range is True
    assert back.acquisition.continuous is False
    assert back.scene.dut_inserted is False and back.scene.tone_on is True
    assert back.scene.dut_order == 5 and isinstance(back.scene.dut_order, int)
    assert back.hardware.model == "SA124B" and back.hardware.serial == 12345678
    assert back.hardware.dll_path.endswith("sa_api.dll")
    assert back.hardware.attach_tg is False


def test_missing_sections_keep_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[sweep]\nspan_hz = 5e6\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.sweep.span_Hz == 5e6
    assert cfg.scene.tone_dBm == Config().scene.tone_dBm
    assert cfg.hardware.model == "auto"


def test_config_file_is_utf8(tmp_path):
    cfg = Config()
    cfg.hardware.dll_path = "C:/Mess\u00e4ger/sa_api.dll"          # a non-ASCII folder name
    path = tmp_path / "u.ini"
    cfg.save(str(path))
    assert Config.load(str(path)).hardware.dll_path == cfg.hardware.dll_path
