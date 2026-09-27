"""Config round-trips through .ini, including the bool and str fields."""

from gsp818.config import Config


def test_round_trip(tmp_path):
    cfg = Config()
    cfg.sweep.points = 401
    cfg.sweep.rbw_Hz = 1234.5
    cfg.sweep.rbw_auto = False                  # bool("False") is True -- must survive
    cfg.sweep.preamp = True
    cfg.sweep.detector = "pos_peak"
    cfg.acquisition.continuous = False
    cfg.tracking.tg_on = True
    cfg.tracking.level_dBm = -12.5
    cfg.bench.dut = "lowpass"
    cfg.bench.carriers = "50e6:-30"
    cfg.bench.dut_order = 5
    cfg.hardware.resource = "TCPIP0::10.0.0.5::inst0::INSTR"
    cfg.hardware.sweep_mode = "single"
    path = tmp_path / "gsp818.ini"
    cfg.save(str(path))

    back = Config.load(str(path))
    assert back.sweep.points == 401 and isinstance(back.sweep.points, int)
    assert back.sweep.rbw_Hz == 1234.5
    assert back.sweep.rbw_auto is False and back.sweep.vbw_auto is True
    assert back.sweep.preamp is True and back.sweep.detector == "pos_peak"
    assert back.acquisition.continuous is False
    assert back.tracking.tg_on is True and back.tracking.level_dBm == -12.5
    assert back.bench.dut == "lowpass" and back.bench.carriers == "50e6:-30"
    assert back.bench.dut_order == 5 and isinstance(back.bench.dut_order, int)
    assert back.hardware.resource.startswith("TCPIP0::")
    assert back.hardware.sweep_mode == "single"


def test_missing_sections_keep_defaults(tmp_path):
    path = tmp_path / "partial.ini"
    path.write_text("[sweep]\npoints = 201\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.sweep.points == 201
    assert cfg.tracking.tg_on is False
    assert cfg.bench.dut_center_Hz == Config().bench.dut_center_Hz


def test_defaults_match_the_data_sheet():
    lim = Config().limits
    assert (lim.freq_min_Hz, lim.freq_max_Hz) == (9e3, 1.8e9)
    assert (lim.rbw_min_Hz, lim.rbw_max_Hz) == (10.0, 3e6)
    assert (lim.tg_level_min_dBm, lim.tg_level_max_dBm) == (-30.0, 0.0)
    assert lim.atten_max_dB == 40.0
