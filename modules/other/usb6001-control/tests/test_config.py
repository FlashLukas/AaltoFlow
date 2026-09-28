"""Config: .ini round trip (per-channel sections, bools, directions), the
validation rules of sanitise(), and the wire dict round trip. No hardware."""

from usb6001.config import (DIO_LINES, Config, _cast, line_index, line_id,
                            daqmx_line, sanitise)
from usb6001.net.protocol import apply_config_dict, config_to_dict


def test_defaults_are_safe():
    cfg = Config()
    # every digital line an INPUT by default: nothing is driven on a fresh install
    assert [d.direction for d in cfg.dio.lines] == ["in"] * 13
    assert all(d.initial == "leave" and d.safe_state == "leave" for d in cfg.dio.lines)
    assert [ch.enabled for ch in cfg.ai.channels] == [True] * 4 + [False] * 4
    assert len(cfg.dio.lines) == len(DIO_LINES) == 13
    assert sanitise(cfg) == []                 # the defaults need no fixing


def test_ini_round_trip_including_bools_and_directions(tmp_path):
    cfg = Config()
    cfg.ai.channels[0].enabled = False          # a bool that must come back False
    cfg.ai.channels[7].enabled = True           # partner of ai3 (RSE): allowed
    cfg.ai.channels[2].terminal = "DIFF"
    cfg.ai.channels[2].unit, cfg.ai.channels[2].slope, cfg.ai.channels[2].offset = "mT", 50.0, -1.5
    cfg.ai.channels[2].name = "Hall probe"
    cfg.ao.channels[1].min_V, cfg.ao.channels[1].max_V = 0.0, 5.0
    cfg.dio.lines[4].direction = "out"
    cfg.dio.lines[4].initial = "high"
    cfg.dio.lines[5].direction = "out"
    cfg.dio.lines[5].safe_state = "low"
    cfg.dio.lines[12].direction = "unused"
    cfg.hardware.sim_ai_loopback = False
    cfg.hardware.device = "Dev3"
    cfg.ai.rate_Hz = 500.0
    p = tmp_path / "usb6001.ini"
    cfg.save(str(p))
    text = p.read_text(encoding="utf-8")
    assert "[dio.p0.4]" in text and "[ai.ai2]" in text and "[ao.ao1]" in text

    back = Config.load(str(p))
    assert config_to_dict(back) == config_to_dict(cfg)
    assert back.ai.channels[0].enabled is False
    assert back.hardware.sim_ai_loopback is False


def test_ini_is_forgiving_about_spelling(tmp_path):
    p = tmp_path / "x.ini"
    p.write_text("[dio.p0.1]\ndirection = OUT\ninitial = High\n"
                 "[ai.ai1]\nterminal = diff\nenabled = False\n", encoding="utf-8")
    cfg = Config.load(str(p))
    assert cfg.dio.lines[1].direction == "out"
    assert cfg.dio.lines[1].initial == "high"
    assert cfg.ai.channels[1].terminal == "DIFF"
    assert cfg.ai.channels[1].enabled is False       # "False" is False (gotcha #3)


def test_bool_cast():
    for raw in ("False", "false", "0", "no", "off", ""):
        assert _cast(raw, "bool") is False
    for raw in ("True", "1", "yes", "on"):
        assert _cast(raw, "bool") is True


def test_diff_disables_its_partner_and_is_refused_above_ai3():
    cfg = Config()
    cfg.ai.channels[0].terminal = "DIFF"
    cfg.ai.channels[4].enabled = True            # the - input of ai0
    cfg.ai.channels[5].terminal = "DIFF"         # impossible: ai5 is a - input
    msgs = sanitise(cfg)
    assert cfg.ai.channels[4].enabled is False
    assert cfg.ai.channels[5].terminal == "RSE"
    assert any("ai4 disabled" in m for m in msgs)
    assert any("ai5" in m and "DIFF" in m for m in msgs)


def test_bad_values_fall_back_to_harmless_ones():
    cfg = Config()
    cfg.dio.lines[3].direction = "output"        # a typo
    cfg.dio.lines[3].initial = "hi"
    cfg.ai.channels[0].terminal = "single"
    msgs = sanitise(cfg)
    assert cfg.dio.lines[3].direction == "unused"   # touches nothing
    assert cfg.dio.lines[3].initial == "leave"
    assert cfg.ai.channels[0].terminal == "RSE"
    assert len(msgs) == 3


def test_ao_limits_and_rate_are_kept_inside_the_card():
    cfg = Config()
    cfg.ao.channels[0].min_V, cfg.ao.channels[0].max_V = 3.0, -30.0   # swapped + too wide
    cfg.ai.rate_Hz = 10000.0                     # x 4 channels > 20 kS/s
    cfg.ai.samples_per_read = 5000               # 5000 / 5000 Hz = 1 s: fine
    sanitise(cfg)
    assert (cfg.ao.channels[0].min_V, cfg.ao.channels[0].max_V) == (-10.0, 3.0)
    assert cfg.ai.rate_Hz == 5000.0
    cfg.ai.samples_per_read = 10000              # 2 s at 5 kHz: too long
    sanitise(cfg)
    assert cfg.ai.samples_per_read == 5000


def test_wire_dict_round_trip_edits_in_place():
    cfg = Config()
    ref = cfg.dio.lines[7]                       # someone holds a reference
    d = config_to_dict(cfg)
    d["dio"]["lines"][7]["direction"] = "out"
    d["ai"]["channels"][1]["enabled"] = "False"  # a string arriving over the wire
    d["hardware"]["poll_hz"] = "10"
    d["nonsense"] = {"x": 1}                     # ignored, not fatal
    apply_config_dict(cfg, d)
    assert ref.direction == "out"                # same object, updated in place
    assert cfg.ai.channels[1].enabled is False
    assert cfg.hardware.poll_hz == 10.0


def test_line_helpers():
    assert line_index("p0.4") == 4
    assert line_index("P1.2") == 10
    assert line_index("Dev1/port2/line0") == 12
    assert line_index(3) == 3
    assert line_id(10) == "p1_2"
    assert daqmx_line("Dev1", 10) == "Dev1/port1/line2"
    for bad in ("p3.0", 13, "x"):
        try:
            line_index(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
