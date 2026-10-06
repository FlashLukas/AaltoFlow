"""The service remembers the sweep window across a restart (remember.py).

Lab PC, 2026-10-06: a restart reset the window to the config defaults. The
analyser keeps no settings to adopt, so the service must remember -- without
breaking the start-up rule (nothing is written to the analyser at start).
"""

from __future__ import annotations

import configparser
import time

import pytest

from fake_sa_api import FakeSaApi
from signalhound.backends.sa_api import SaApiAnalyzer
from signalhound.config import Config
from signalhound.remember import REMEMBERED, SweepMemory, memory_path
from signalhound.sim_system import build_sim_system
from signalhound.spectrum import SpectrumAnalyzer


def _brain(path, readonly=False, **mem_kw):
    """A brain on the fake DLL (an SA124B: 12.4 GHz, RBW up to 6 MHz) that
    remembers in `path`, with its events collected."""
    cfg = Config()
    dll = FakeSaApi(device_type=4, bins=101, readonly=readonly)
    mem = SweepMemory(path, **mem_kw)
    v = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=dll), cfg, memory=mem)
    events = []
    v._on_event = lambda lvl, msg: events.append((lvl, msg))
    return v, dll, mem, events


def _window(st):
    return (st.center_Hz, st.span_Hz, st.ref_level_dBm, st.rbw_Hz, st.vbw_Hz,
            st.reject, st.detector, st.averages)


def test_the_operator_window_survives_a_restart_and_start_writes_nothing(tmp_path):
    path = tmp_path / "signalhound_sweep.ini"
    v, _dll, _mem, _ = _brain(path)
    v.start(run=False)
    try:
        # the window of the lab report: 480 MHz - 12.4 GHz, RBW 6 MHz, VBW 100 kHz
        v.set_start_stop(480e6, 12.4e9)
        v.set_rbw(6e6)
        v.set_vbw(100e3)
        v.set_ref_level(-35.0)
        v.set_reject(False)
        v.set_detector("peak")
        v.set_averages(4)
        before = _window(v.status())
    finally:
        v.shutdown()                                       # flushes what the throttle held
    assert before[0] == pytest.approx(6.44e9) and before[3] == 6e6

    # a NEW brain from the same folder, on an analyser that raises on any write
    v2, dll2, _mem2, events = _brain(path, readonly=True)
    v2.start(run=False)
    try:
        for _ in range(3):
            v2.step()                                      # idle passes of the sweep thread
        st = v2.status()
        assert _window(st) == before
        assert dll2.writes() == [] and dll2.mode == -1      # start-up wrote NOTHING
        assert st.configured is False and st.points == 0
        assert any("remembered sweep settings loaded" in m for _l, m in events)
        dll2.readonly = False
        v2.set_continuous(True)                            # the first deliberate sweep ...
        v2.step()
        assert dll2.center == pytest.approx(6.44e9)        # ... uses the remembered window
        assert dll2.rbw == 6e6
    finally:
        dll2.readonly = False
        v2.shutdown()


def test_a_corrupt_file_falls_back_to_the_defaults_with_one_warning(tmp_path):
    defaults = Config().sweep
    for text in ("this is not an ini file at all\n",
                 "[sweep]\ncenter_Hz = nan\nspan_Hz = 1e6\n",
                 "[sweep]\ncenter_Hz = twelve\n",
                 "[other]\nx = 1\n"):
        path = tmp_path / "signalhound_sweep.ini"
        path.write_text(text, encoding="utf-8")
        v, _dll, _mem, events = _brain(path)
        v.start(run=False)
        try:
            st = v.status()
            assert st.center_Hz == defaults.center_Hz and st.span_Hz == defaults.span_Hz
            warns = [m for lvl, m in events if lvl == "warn" and "unreadable" in m]
            assert len(warns) == 1, (text, events)
        finally:
            v.shutdown()


def test_a_missing_file_is_one_info_line_and_the_defaults(tmp_path):
    v, _dll, _mem, events = _brain(tmp_path / "none.ini")
    v.start(run=False)
    try:
        assert v.status().center_Hz == Config().sweep.center_Hz
        assert sum("no remembered sweep settings" in m for _l, m in events) == 1
    finally:
        v.shutdown()
    assert not (tmp_path / "none.ini").exists()            # nothing changed: nothing written


def test_continuous_and_the_tracking_generator_are_not_remembered(tmp_path):
    path = tmp_path / "signalhound_sweep.ini"
    v, _dll, _mem, _ = _brain(path)
    v.start(run=False)
    try:
        v.set_continuous(True)
        v.tg_cw(on=True, freq_hz=1e9, level_dbm=-20.0)
        v.cfg.hardware.tg_park_dbm = -15.0                 # a TG owner setting, over set_config
        v.apply_config()
        v.set_span(50e6)
    finally:
        v.shutdown()
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    assert cp.sections() == ["sweep"]
    assert set(cp["sweep"]) == {k.lower() for k in REMEMBERED}

    v2, _dll2, _mem2, _ = _brain(path)
    v2.start(run=False)
    try:
        st = v2.status()
        assert st.span_Hz == 50e6                          # the sweep setting came back
        assert st.continuous is False                      # start-up rule: not sweeping
        assert st.tg_mode == "unknown" and st.tg_cw_on is False
        assert v2.cfg.hardware.tg_park_dbm == Config().hardware.tg_park_dbm
    finally:
        v2.shutdown()


def test_writes_are_throttled_and_the_last_value_always_lands(tmp_path):
    path = tmp_path / "s.ini"
    now = [100.0]
    v, _dll, mem, _ = _brain(path, min_interval_s=60.0, clock=lambda: now[0])
    v.start(run=False)
    try:
        for i in range(50):                                # a spin box, one value per keystroke
            v.set_center(1.0e9 + i * 1e6)
        assert mem.writes == 1                             # the first at once, the rest held
        now[0] += 61.0
        v.set_center(2.0e9)                                # interval over: written at once
        assert mem.writes == 2
        v.set_center(2.5e9)                                # held by the throttle ...
        assert mem.writes == 2
        v.apply_config()                                   # no change: nothing extra queued
    finally:
        v.shutdown()                                       # ... and written on shutdown
    assert mem.writes == 3
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    assert float(cp["sweep"]["center_Hz"]) == 2.5e9


def test_a_held_back_value_is_written_when_the_interval_is_up(tmp_path):
    path = tmp_path / "s.ini"
    v, _dll, mem, _ = _brain(path, min_interval_s=0.2)
    v.start(run=False)
    try:
        v.set_span(10e6)
        v.set_span(20e6)                                   # held back
        assert mem.writes == 1
        deadline = time.monotonic() + 3.0
        while mem.writes < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert mem.writes == 2                             # the timer wrote it, no shutdown
        cp = configparser.ConfigParser()
        cp.read(path, encoding="utf-8")
        assert float(cp["sweep"]["span_Hz"]) == 20e6
    finally:
        v.shutdown()


def test_a_failed_write_is_a_warning_not_a_crash(tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("x", encoding="utf-8")
    v, _dll, _mem, events = _brain(blocker / "s.ini")      # its folder is a FILE
    v.start(run=False)
    try:
        v.set_span(10e6)                                   # must not raise
        assert v.status().span_Hz == 10e6
        assert any(lvl == "warn" and "could not remember" in m for lvl, m in events)
    finally:
        v.shutdown()
    assert not list(tmp_path.glob("*.tmp"))                # no temporary file left behind


def test_the_simulator_remembers_in_its_own_file(tmp_path):
    assert memory_path(tmp_path, simulated=False).name == "signalhound_sweep.ini"
    assert memory_path(tmp_path, simulated=True).name == "signalhound_sweep_sim.ini"
    cfg = Config()
    v, _ = build_sim_system(cfg, realtime=False, seed=1)
    v.attach_memory(SweepMemory(memory_path(tmp_path, simulated=True)))
    v.start(run=False)
    try:
        v.set_center(1.2e9)
    finally:
        v.shutdown()
    cfg2 = Config()
    v2, _ = build_sim_system(cfg2, realtime=False, seed=1)
    v2.attach_memory(SweepMemory(memory_path(tmp_path, simulated=True)))
    v2.start(run=False)
    try:
        assert v2.status().center_Hz == 1.2e9
    finally:
        v2.shutdown()
    assert not memory_path(tmp_path, simulated=False).exists()
