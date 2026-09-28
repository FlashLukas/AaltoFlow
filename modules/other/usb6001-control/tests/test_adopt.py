"""ADOPT ON START (Lukas, 2026-09-27: read the instrument state at start,
change nothing). Checked against the simulator's write log."""

import math

from usb6001.sim_system import build_sim_system, demo_config


def test_start_writes_nothing_and_ao_is_unknown():
    cfg = demo_config()
    daq, sim = build_sim_system(cfg, ao_start=(3.3, -1.0),
                                do_start=[False] * 4 + [True, False, True, False] + [False] * 5)
    daq.start(poll=False)
    assert sim.writes == []                              # not one AO or DO write
    st = daq.status()
    # the 6001 cannot read AO back: unknown until set, NOT the 0 V of a config
    assert st.ao_known == [False, False]
    assert all(math.isnan(v) for v in st.ao_V)
    assert sim._ao == [3.3, -1.0]                        # the card kept its outputs
    # output lines ADOPTED from the card's readback
    assert st.dio[4:8] == [True, False, True, False]
    daq.shutdown()
    assert sim.writes == []                              # safe_state "leave" everywhere


def test_output_level_unknown_when_card_cannot_read_it():
    cfg = demo_config()
    daq, sim = build_sim_system(cfg)
    sim.do_readable = False
    events = []
    daq._on_event = lambda lvl, msg: events.append((lvl, msg))
    daq.start(poll=False)
    assert daq.status().dio[4:8] == [None] * 4
    assert any("unknown until first write" in m for _, m in events)
    daq.shutdown()


def test_initial_low_high_writes_exactly_that():
    cfg = demo_config()
    cfg.dio.lines[4].initial = "high"
    cfg.dio.lines[6].initial = "low"
    daq, sim = build_sim_system(cfg, do_start=[True] * 13)
    daq.start(poll=False)
    assert sim.writes == [("do", 4, True), ("do", 6, False)]
    st = daq.status()
    assert st.dio[4] is True and st.dio[6] is False and st.dio[5] is True   # 5: adopted
    daq.shutdown()


def test_initial_on_an_input_line_is_ignored():
    cfg = demo_config()
    cfg.dio.lines[0].initial = "high"                    # p0.0 is an input
    daq, sim = build_sim_system(cfg)
    daq.start(poll=False)
    assert sim.writes == []
    daq.shutdown()
