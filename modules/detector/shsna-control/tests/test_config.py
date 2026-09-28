"""Config round-trips through .ini, including the bool and str fields."""

from shsna.config import Config


def test_round_trip(tmp_path):
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz = 123.5e6, 2.5e9
    cfg.sweep.points = 1001
    cfg.sweep.rbw_Hz = 3e3
    cfg.sweep.averages = 7
    cfg.acquisition.continuous = True
    cfg.sim.dut_inserted = False                # bool("False") is True -- must survive
    cfg.sim.dut_order = 5
    cfg.sim.pad_dB = 30.0
    cfg.hardware.owner_host = "192.168.1.42"
    cfg.hardware.owner_cmd_port = 15999
    cfg.ui.theme = "light"
    path = tmp_path / "shsna.ini"
    cfg.save(str(path))

    back = Config.load(str(path))
    assert (back.sweep.start_Hz, back.sweep.stop_Hz) == (123.5e6, 2.5e9)
    assert back.sweep.points == 1001 and back.sweep.rbw_Hz == 3e3
    assert back.sweep.averages == 7 and isinstance(back.sweep.averages, int)
    assert back.acquisition.continuous is True
    assert back.sim.dut_inserted is False and back.sim.tg_attached is True
    assert back.sim.dut_order == 5 and isinstance(back.sim.dut_order, int)
    assert back.sim.pad_dB == 30.0
    assert back.hardware.owner_host == "192.168.1.42" and back.hardware.owner_cmd_port == 15999
    assert back.ui.theme == "light"


def test_missing_sections_keep_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[sweep]\npoints = 201\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.sweep.points == 201
    assert cfg.sim.pad_dB == Config().sim.pad_dB
    assert cfg.hardware.owner_cmd_port == 5587          # the signalhound service


def test_defaults_are_the_lab_setup():
    cfg = Config()
    assert cfg.sim.pad_dB == 20.0                       # TG -> 20 dB pad -> SA
    assert cfg.acquisition.continuous is False          # nothing sweeps at start
    assert cfg.limits.points_max == 1001                # the SA API's TG maximum
    assert not hasattr(cfg.sweep, "level_dBm")          # the TG44A ignores it in sweep mode
    assert cfg.limits.freq_max_Hz == 4.4e9
