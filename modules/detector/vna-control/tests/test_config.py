"""Config round-trips through .ini, including the bool and str fields."""

from vna.config import Config


def test_round_trip(tmp_path):
    cfg = Config()
    cfg.sweep.points = 401
    cfg.sweep.ifbw_Hz = 1234.5
    cfg.acquisition.continuous = False          # bool("False") is True -- must survive
    cfg.field.source = "manual"
    cfg.sample.alpha = 2.5e-4
    cfg.sample.geometry = "out_of_plane"
    cfg.line.delay_ns = 0.0
    cfg.sweep.sparam = "S12"
    cfg.field.mag2d_pub_port = 15999
    cfg.field.manual_angle_deg = -45.0
    cfg.sample.hk_mT, cfg.sample.easy_axis_deg = 5.0, 30.0
    cfg.hardware.cal_set = "{A1B2C3}"
    cfg.hardware.data_format = "ASCII"
    path = tmp_path / "vna.ini"
    cfg.save(str(path))

    back = Config.load(str(path))
    assert back.sweep.sparam == "S12" and back.field.mag2d_pub_port == 15999
    assert back.field.manual_angle_deg == -45.0
    assert (back.sample.hk_mT, back.sample.easy_axis_deg) == (5.0, 30.0)
    assert back.hardware.cal_set == "{A1B2C3}" and back.hardware.data_format == "ASCII"
    assert back.hardware.visa_resource == "N5222A"
    assert back.sweep.points == 401 and isinstance(back.sweep.points, int)
    assert back.sweep.ifbw_Hz == 1234.5
    assert back.acquisition.continuous is False
    assert back.field.source == "manual"
    assert back.sample.alpha == 2.5e-4
    assert back.sample.geometry == "out_of_plane"
    assert back.line.delay_ns == 0.0


def test_missing_sections_keep_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[sweep]\npoints = 201\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.sweep.points == 201
    assert cfg.sample.ms_mT == Config().sample.ms_mT
