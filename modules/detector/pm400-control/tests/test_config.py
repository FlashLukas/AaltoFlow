"""Config round-trips through the .ini, bools included (bool("False") is True)."""

from pm400.config import Config


def test_defaults():
    cfg = Config()
    assert cfg.sensor.auto_range is True
    # adopt-on-start rule (2026-09-27): there is no option to push at start
    assert not hasattr(cfg.hardware, "push_on_start")
    assert cfg.hardware.channel == 1
    assert cfg.acquisition.readings >= 1
    assert cfg.sim.head == "photodiode"
    # a reading blocks setters for its averaging time: keep that short
    assert cfg.limits.avg_time_max_s <= 1.0


def test_ini_round_trip(tmp_path):
    cfg = Config()
    cfg.sensor.wavelength_nm = 1064.0
    cfg.sensor.auto_range = False
    cfg.sensor.range_W = 0.02
    cfg.sensor.range_J = 0.0015
    cfg.sensor.avg_time_s = 0.25
    cfg.acquisition.readings = 12
    cfg.acquisition.settle_s = 5.0
    cfg.hardware.resource = "USB0::0x1313::0x807D::000000000::INSTR"
    cfg.sim.head = "pyro"
    cfg.sim.rep_rate_Hz = 20.0
    cfg.ui.theme = "light"
    path = tmp_path / "pm400.ini"
    cfg.save(str(path))

    back = Config.load(str(path))
    assert back.sensor.wavelength_nm == 1064.0
    assert back.sensor.auto_range is False          # the classic bool trap
    assert back.sensor.range_W == 0.02 and back.sensor.range_J == 0.0015
    assert back.sensor.avg_time_s == 0.25
    assert back.acquisition.readings == 12 and back.acquisition.settle_s == 5.0
    assert back.hardware.resource.endswith("000000000::INSTR")
    assert back.sim.head == "pyro" and back.sim.rep_rate_Hz == 20.0
    assert back.ui.theme == "light"


def test_partial_ini_keeps_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[sensor]\nwavelength_nm = 633\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.sensor.wavelength_nm == 633.0
    assert back.limits.wavelength_max_nm == Config().limits.wavelength_max_nm
    assert back.sim.head == "photodiode"


def test_every_group_travels_over_the_wire():
    """A group missing from config_to_dict / apply_config_dict would silently
    not travel (gotcha #4)."""
    from pm400.net.protocol import apply_config_dict, config_to_dict
    d = config_to_dict(Config())
    assert set(d) == set(Config._GROUPS)
    d["sim"]["head"] = "thermal"
    d["acquisition"]["settle_s"] = 3.0
    cfg = Config()
    apply_config_dict(cfg, d)
    assert cfg.sim.head == "thermal" and cfg.acquisition.settle_s == 3.0


def test_old_ini_with_push_on_start_still_loads(tmp_path):
    # An .ini saved before 2026-09-27 still carries the removed key; it must be
    # ignored, not crash the service.
    path = tmp_path / "old.ini"
    path.write_text("[hardware]\npush_on_start = True\nchannel = 1\n", encoding="utf-8")
    back = Config.load(str(path))
    assert back.hardware.channel == 1
    assert not hasattr(back.hardware, "push_on_start")
