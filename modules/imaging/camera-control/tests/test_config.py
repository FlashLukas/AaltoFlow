"""Config INI round-trip, including the bool trap (blueprint §4, §9)."""

from camera.config import Config, load_config, save_config


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.camera.frame_rate = 12.5
    cfg.image.objective_name = "50x - Zeiss NA 0.8"
    cfg.image.pixel_size_x_um = 0.1652
    cfg.scanning.points_x = 5
    cfg.scanning.dx_um = 2.5
    cfg.stabilizer.gain = 0.25
    cfg.autofocus.mechanism = "edges"
    cfg.image.symmetry = "horizontal"

    path = tmp_path / "cam.ini"
    save_config(cfg, str(path))
    back = load_config(str(path))

    assert back.camera.frame_rate == 12.5
    assert back.image.objective_name == "50x - Zeiss NA 0.8"
    assert back.image.pixel_size_x_um == 0.1652
    assert back.scanning.points_x == 5
    assert back.scanning.dx_um == 2.5
    assert back.stabilizer.gain == 0.25
    assert back.autofocus.mechanism == "edges"
    assert back.image.symmetry == "horizontal"


def test_bool_survives_both_ways(tmp_path):
    # The classic trap: bool("False") is True -> must be parsed explicitly.
    for value in (True, False):
        cfg = Config()
        cfg.stabilizer.move_with_x = value
        cfg.image.clip_enabled = value
        cfg.limits.enforce = value
        p = tmp_path / f"b_{value}.ini"
        save_config(cfg, str(p))
        back = load_config(str(p))
        assert back.stabilizer.move_with_x is value
        assert back.image.clip_enabled is value
        assert back.limits.enforce is value


def test_ui_theme_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.ui.theme == "dark"          # default
    cfg.ui.theme = "light"
    p = tmp_path / "ui.ini"
    save_config(cfg, str(p))
    assert load_config(str(p)).ui.theme == "light"
    # an invalid theme is sanitised back to dark on load
    cfg.ui.theme = "banana"
    save_config(cfg, str(p))
    assert load_config(str(p)).ui.theme == "dark"


def test_unknown_enum_sanitised(tmp_path):
    cfg = Config()
    cfg.autofocus.mechanism = "nonsense"
    p = tmp_path / "x.ini"
    save_config(cfg, str(p))
    back = load_config(str(p))
    assert back.autofocus.mechanism == "spot_area"
