"""The LibreVNA and Harogic PNB-60 backend against FAKE programs
(tests/fake_sweepserver.py): LibreVNA-GUI and VNStudio.

What this proves: the SCPI sequence the backend sends (both guides' single-sweep
recipe: settings + read-back, VNA:ACQ:SINGLE TRUE, poll "done?", VNA:TRAC:DATA?),
that the [f, re, im] reply is parsed, that the grid is checked point by point,
and that errors -- *ESR? bits and VNStudio's stray error lines -- stop a sweep.
What it cannot prove: that the real programs agree (# VERIFY in librevna.py).
"""

import numpy as np
import pytest

from fake_sweepserver import FakeSweepServer
from vna.analyzer import Analyzer
from vna.backends import real_backend, resource_field
from vna.backends.librevna import (DIALECTS, HAROGIC, LIBREVNA, TRACE, SweepServerVna,
                                   limit_envelope, parse_trace)
from vna.config import Config
from vna.field import FieldReading

F = FieldReading(50.0, True, "manual", 0.0)
BOTH = ["librevna", "harogic"]


def _backend(driver="librevna", cfg=None, **fake_kw):
    cfg = cfg or Config()
    cfg.hardware.driver = driver
    fake = FakeSweepServer(driver, **fake_kw)
    return SweepServerVna(cfg, DIALECTS[driver], resource=fake, sleep=lambda s: None), fake, cfg


def _writes(log):
    """The commands in a log that are not queries (every query has a '?')."""
    return [c for c in log if "?" not in c]


@pytest.mark.parametrize("driver", BOTH)
def test_open_only_looks_and_changes_nothing(driver):
    """Lukas's rule (2026-09-27): connecting reads the program, it sets nothing."""
    b, fake, _ = _backend(driver)
    b.open()
    st = b.read_state()
    assert fake.read_termination == "\n" and fake.write_termination == "\n"
    assert _writes(fake.log) == []
    assert (st["start_Hz"], st["stop_Hz"], st["points"], st["ifbw_Hz"], st["power_dBm"]) == (
        1e6, 6e9, 501, 10e3, -10.0)
    assert st["continuous"] is False and st["device_limits"]["freq_max_Hz"] == 6e9
    b.close()
    assert _writes(fake.log) == [], "close of an analyser only looked at writes nothing"
    assert fake.closed


@pytest.mark.parametrize("driver", BOTH)
def test_no_analyser_behind_the_program_is_refused_at_open(driver):
    b, fake, _ = _backend(driver, connected=False)
    with pytest.raises(RuntimeError, match="no analyser connected"):
        b.open()
    assert _writes(fake.log) == []


def test_a_sweep_on_librevna_uses_its_own_trace():
    b, fake, _ = _backend("librevna")
    b.open()
    b.read_state()
    fake.log.clear()
    f = np.linspace(1e9, 3e9, 101)
    b.start_sweep(f, 1e3, -20.0, "S21", F)
    log = list(fake.log)
    for cmd in ("DEV:MODE VNA", "VNA:SWEEP FREQUENCY", "VNA:ACQ:AVG 1", f"VNA:TRAC:NEW {TRACE}",
                "VNA:FREQ:START 1000000000", "VNA:FREQ:STOP 3000000000", "VNA:ACQ:POINTS 101",
                "VNA:ACQ:IFBW 1000", "VNA:STIM:LVL -20", f"VNA:TRAC:PARAM {TRACE} S21"):
        assert cmd in log, cmd
    assert "VNA:SWEEPTYPE LIN" not in log                 # VNStudio only
    assert log.index("VNA:ACQ:POINTS 101") < log.index("VNA:ACQ:POINTS?")
    assert log[-1] == "VNA:ACQ:SINGLE TRUE" and fake.sweeps == 1   # trigger LAST, once
    assert fake.traces[TRACE] == "S21" and fake.avg == 1
    z, meta = b.finish_sweep()
    assert fake.log[-1] == f"VNA:TRAC:DATA? {TRACE}"
    assert fake.log.count("VNA:ACQ:FIN?") == 2            # polled until done
    np.testing.assert_allclose(z, (np.arange(101) + 1.0) * (0.25 + 0.5j))
    assert meta["ifbw_actual_Hz"] == 1e3 and meta["power_actual_dBm"] == -20.0


def test_a_sweep_on_the_pnb60_reads_the_sparameter_and_its_sweep_time():
    b, fake, _ = _backend("harogic")
    b.open()
    b.read_state()
    fake.log.clear()
    f = np.linspace(1e9, 3e9, 101)
    b.start_sweep(f, 1e3, -20.0, "S12", F)
    log = list(fake.log)
    assert "VNA:SWEEPTYPE LIN" in log and "DEV:MODE VNA" not in log
    assert not any(c.startswith("VNA:TRAC:") for c in log)
    assert b.sweep_time_s(101, 1e3) == pytest.approx(0.1801731)   # VNStudio's own figure
    assert b.sweep_time_s(201, 1e3) != pytest.approx(0.1801731)   # other settings: the rule
    b.finish_sweep()
    assert fake.log[-1] == "VNA:TRAC:DATA? S12"
    assert fake.log.count("VNA:ACQ:RUN?") == 2


@pytest.mark.parametrize("driver", BOTH)
def test_unchanged_settings_are_not_rewritten(driver):
    b, fake, _ = _backend(driver)
    b.open()
    f = np.linspace(1e9, 2e9, 11)
    b.start_sweep(f, 1e3, -20.0, "S21", F); b.finish_sweep()
    fake.log.clear()
    b.start_sweep(f, 1e3, -20.0, "S21", F)
    assert fake.log == ["VNA:ACQ:SINGLE TRUE"]


def test_moving_the_band_up_writes_stop_first():
    b, fake, _ = _backend("librevna")
    b.open()
    b.read_state()                                        # holds 1 MHz - 6 GHz
    b.start_sweep(np.linspace(1e8, 2e8, 11), 1e3, -20.0, "S21", F)
    fake.log.clear()
    b.start_sweep(np.linspace(3e8, 4e8, 11), 1e3, -20.0, "S21", F)
    w = _writes(fake.log)
    assert w.index("VNA:FREQ:STOP 400000000") < w.index("VNA:FREQ:START 300000000")


@pytest.mark.parametrize("driver", BOTH)
def test_points_the_program_did_not_take_are_refused(driver):
    b, fake, _ = _backend(driver, snap_points=lambda n: n - 1)
    b.open()
    with pytest.raises(RuntimeError, match="did not take the sweep"):
        b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -20.0, "S21", F)
    assert fake.sweeps == 0


@pytest.mark.parametrize("driver", BOTH)
def test_a_trace_measured_at_other_frequencies_is_refused(driver):
    """The reply names every point's frequency: a trace that would be filed
    under the wrong ones is refused, not stored."""
    b, fake, _ = _backend(driver, freq_offset_Hz=5e3)
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -20.0, "S21", F)
    with pytest.raises(RuntimeError, match="other frequencies"):
        b.finish_sweep()


def test_an_esr_error_stops_the_sweep_before_it_is_triggered():
    b, fake, _ = _backend("librevna")
    b.open()
    fake.esr = 0x10                       # a setting the program refused
    with pytest.raises(RuntimeError, match="execution error"):
        b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -20.0, "S21", F)
    assert fake.sweeps == 0


def test_a_stray_error_line_from_vnstudio_does_not_shift_the_replies():
    """VNStudio may ANSWER a failed command. That line is read in front of the
    *ESR? number, reported as the error -- and the next reply is the right one."""
    b, fake, _ = _backend("harogic")
    b.open()
    fake.unsolicited.append("GATEWAY_EXECUTION_ERROR")
    fake.esr = 0x10
    with pytest.raises(RuntimeError, match="GATEWAY_EXECUTION_ERROR"):
        b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -20.0, "S21", F)
    assert fake.sweeps == 0
    # back in step: the retry re-writes everything and sweeps
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -20.0, "S21", F)
    z, _ = b.finish_sweep()
    assert z.size == 11 and fake.sweeps == 1


def test_a_sweep_that_never_ends_times_out():
    clock = iter(range(0, 10_000, 5))
    cfg = Config()
    cfg.hardware.driver = "librevna"
    cfg.hardware.timeout_s = 2.0
    fake = FakeSweepServer("librevna", sweep_polls=10_000)
    b = SweepServerVna(cfg, LIBREVNA, resource=fake, clock=lambda: float(next(clock)),
                       sleep=lambda s: None)
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e5, -20.0, "S21", F)
    with pytest.raises(TimeoutError):
        b.finish_sweep()


@pytest.mark.parametrize("driver", BOTH)
def test_close_hands_the_program_back_sweeping_continuously(driver):
    b, fake, _ = _backend(driver)
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -20.0, "S21", F)
    b.abort_sweep()
    b.close()
    assert fake.log[-1] == "VNA:ACQ:SINGLE FALSE" and not fake.single
    b.close()                                             # twice is fine


def test_a_different_model_is_a_warning_not_a_failure():
    b, _, _ = _backend("harogic", idn="Harogic,SAE-200,1,1")
    b.open()
    assert b.warnings and "PNB-60" in b.warnings[0]


def test_parse_trace_refuses_a_broken_reply():
    f, z = parse_trace("[1e6,0.5,-0.25],[2e6,1,0]")
    np.testing.assert_allclose(f, [1e6, 2e6])
    np.testing.assert_allclose(z, [0.5 - 0.25j, 1.0])
    with pytest.raises(RuntimeError, match="triples"):
        parse_trace("[1e6,0.5,-0.25],[2e6,1]")
    with pytest.raises(RuntimeError, match="triples"):
        parse_trace("")


@pytest.mark.parametrize("dialect", [LIBREVNA, HAROGIC])
def test_the_envelope_is_pulled_inside_the_analyser(dialect):
    cfg = Config()
    cfg.sweep.stop_Hz = 12e9
    cfg.sweep.points = 10001
    limit_envelope(cfg, dialect)
    env = dialect.envelope
    assert cfg.limits.freq_max_Hz == env["freq_max_Hz"] == cfg.sweep.stop_Hz
    assert cfg.limits.freq_min_Hz == env["freq_min_Hz"]
    assert cfg.limits.points_max == env["points_max"] == cfg.sweep.points
    assert cfg.limits.power_max_dBm == env["power_max_dBm"]
    assert cfg.sweep.start_Hz < cfg.sweep.stop_Hz


@pytest.mark.parametrize("driver,port", [("librevna", 19542), ("harogic", 5025)])
def test_the_driver_picks_its_backend_and_address(driver, port):
    cfg = Config()
    cfg.hardware.driver = driver
    b = real_backend(cfg)
    assert isinstance(b, SweepServerVna) and b.d.key == driver
    assert resource_field(cfg) == DIALECTS[driver].resource_field
    assert b.resource_name == f"TCPIP0::127.0.0.1::{port}::SOCKET"


@pytest.mark.parametrize("driver", BOTH)
def test_the_brain_acquires_averages_and_files_the_field(driver):
    """The whole chain on the fake: brain -> backend -> averaged sample."""
    cfg = Config()
    cfg.hardware.driver = driver
    cfg.field.source = "manual"
    cfg.field.manual_mT = 77.0
    cfg.sweep.averages = 2
    limit_envelope(cfg, DIALECTS[driver])
    fake = FakeSweepServer(driver)
    vna = Analyzer(SweepServerVna(cfg, DIALECTS[driver], resource=fake), cfg)
    vna.start()
    try:
        vna.set_start(1e9); vna.set_stop(2e9); vna.set_points(21)
        vna.set_ifbw(50e3)
        acq = vna.acquire()
        import time
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:
            st = vna.status()
            if st.acq_id == acq and not st.acquiring:
                break
            time.sleep(0.02)
        sample = vna.get_sample()
        assert sample["acq_id"] == acq and sample["field_mT"] == 77.0
        z = np.asarray(vna.get_trace("sample", "s")["s"])
        # sweeps 1 and 2 averaged: (1 + 2) / 2 = 1.5 x the base trace
        np.testing.assert_allclose(z, 1.5 * (np.arange(21) + 1.0) * (0.25 + 0.5j))
        assert fake.sweeps == 2
    finally:
        vna.shutdown()
    assert fake.closed
