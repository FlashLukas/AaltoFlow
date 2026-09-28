"""The brain: clamps, refusals, fresh reads, hw_error, safe states. Offline."""

import math
import threading

import pytest

from usb6001.config import Config
from usb6001.sim_system import build_sim_system, demo_config


def _started(cfg=None, poll=False, **kw):
    cfg = cfg or demo_config()
    daq, sim = build_sim_system(cfg, **kw)
    events = []
    daq._on_event = lambda lvl, msg: events.append((lvl, msg))
    daq.start(poll=poll)
    return daq, sim, events


def test_set_ao_clamps_and_warns():
    daq, sim, ev = _started()
    assert daq.set_ao(1, 7.0) == 5.0                     # demo ao1 limit 0..5 V
    assert sim.writes[-1] == ("ao", 1, 5.0)
    assert daq.status().ao_V[1] == 5.0 and daq.status().ao_known[1]
    assert ev[-1][0] == "warn" and "clamped" in ev[-1][1]
    assert daq.set_ao("ao0", -3.0) == -3.0              # by id
    assert daq.set_ao("Coil drive", 2.0) == 2.0         # by configured name
    assert ev[-1][0] == "info"
    daq.shutdown()


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_set_ao_refuses_non_numbers(bad):
    daq, sim, _ = _started()
    with pytest.raises(ValueError):
        daq.set_ao(0, bad)
    assert sim.writes == []
    daq.shutdown()


def test_failed_ao_write_stores_no_echo():
    """gotcha #40: the echo a scan waits on must mean 'written'."""
    daq, sim, _ = _started()

    def boom(ch, v):
        raise OSError("USB gone")
    sim.write_ao = boom
    with pytest.raises(OSError):
        daq.set_ao(0, 1.0)
    assert daq.status().ao_known[0] is False
    assert math.isnan(daq.status().ao_V[0])
    daq.shutdown()


def test_set_do_only_on_output_lines():
    daq, sim, _ = _started()          # demo: p0.0-3 in, p0.4-7 out, p1.x unused
    daq.set_do("p0.4", True)
    assert daq.status().dio[4] is True and sim.writes[-1] == ("do", 4, True)
    daq.set_do("Shutter", False)                         # by configured name
    assert daq.status().dio[4] is False
    n = len(sim.writes)
    with pytest.raises(ValueError, match="configured as 'in'"):
        daq.set_do("p0.0", True)
    with pytest.raises(ValueError, match="'unused'"):
        daq.set_do("p1.0", True)
    assert len(sim.writes) == n                          # nothing reached the card
    daq.shutdown()


def test_direction_edited_in_config_does_not_apply_live():
    daq, sim, ev = _started()
    daq.cfg.dio.lines[0].direction = "out"               # edit, no restart
    daq.apply_config()
    assert daq.status().restart_pending is True
    assert any("RESTART" in m for _, m in ev)
    with pytest.raises(ValueError):
        daq.set_do("p0.0", True)                         # still an input until restart
    assert daq.status().dio_dir[0] == "in"
    daq.shutdown()


def test_fresh_read_is_taken_after_the_trigger():
    daq, sim, _ = _started(poll=True)
    try:
        daq.set_ao(0, 1.5)
        r = daq.read_ai("ai0")                           # loopback ai0 <- ao0
        assert abs(r["values_V"]["ai0"] - 1.5) < 0.01
        daq.set_ao(0, -2.0)
        r2 = daq.read_ai()                               # all enabled inputs
        assert r2["acq_id"] > r["acq_id"]
        assert abs(r2["values_V"]["ai0"] + 2.0) < 0.01
        assert set(r2["values_V"]) == {"ai0", "ai1", "ai2", "ai3"}
        # the scaled channel: ai2 = 100 mT/V x V
        assert r2["units"]["ai2"] == "mT"
        assert r2["values"]["ai2"] == pytest.approx(100 * r2["values_V"]["ai2"])
    finally:
        daq.shutdown()


def test_acquire_numbers_and_latches_in_one_step():
    daq, sim, _ = _started()
    a = daq.acquire()
    st = daq.status()
    assert st.acq_id == a and st.acquiring is True       # new id + busy together
    daq.poll_once()
    st = daq.status()
    assert st.acquiring is False and st.sample["acq_id"] == a
    assert len(st.sample["ai_V"]) == 8 and math.isnan(st.sample["ai_V"][5])
    daq.shutdown()


def test_read_di_and_refusal_of_non_inputs():
    daq, sim, _ = _started()
    daq.set_do("p0.4", True)
    r = daq.read_di()
    assert r["levels"]["p0.0"] is True                  # loopback: 1st input <- 1st output
    with pytest.raises(ValueError):
        daq.read_di("p0.4")                              # an output, not an input
    with pytest.raises(ValueError):
        daq.read_ai("ai6")                               # not enabled
    daq.shutdown()


def test_hw_error_keeps_last_good_values_one_event_per_episode():
    daq, sim, ev = _started()
    daq.poll_once()
    good = daq.status().ai_V[2]
    sim.fail_reads = True
    for _ in range(5):
        daq.poll_once()
    st = daq.status()
    assert "USB" in st.hw_error
    assert st.ai_V[2] == good                            # last good kept
    assert sum(1 for lvl, m in ev if lvl == "error") == 1
    sim.fail_reads = False
    daq.poll_once()
    assert daq.status().hw_error == ""
    assert any("recovered" in m for _, m in ev)
    daq.shutdown()


def test_fresh_read_times_out_instead_of_serving_old_values():
    daq, sim, _ = _started()
    daq.poll_once()
    sim.fail_reads = True
    with pytest.raises(TimeoutError):
        daq.fresh_sample(timeout_s=0.2)
    daq.shutdown()


def test_status_never_touches_the_hardware():
    daq, sim, _ = _started()
    calls = sim.read_ai_calls
    for _ in range(20):
        daq.status()
    assert sim.read_ai_calls == calls
    daq.shutdown()


def test_shutdown_writes_only_configured_safe_states():
    cfg = demo_config()
    cfg.dio.lines[5].safe_state = "low"
    cfg.dio.lines[6].safe_state = "high"
    daq, sim, _ = _started(cfg)
    sim.writes.clear()
    daq.shutdown()
    assert sim.writes == [("do", 5, False), ("do", 6, True)]   # no AO, no other line
    daq.shutdown()                                       # twice is safe


def test_narrowed_ao_limit_moves_a_known_output_inside():
    daq, sim, _ = _started()
    daq.set_ao(0, 8.0)
    daq.cfg.ao.channels[0].max_V = 2.0
    daq.apply_config()
    assert daq.status().ao_V[0] == 2.0 and sim.writes[-1] == ("ao", 0, 2.0)
    daq.shutdown()


def test_apply_config_saves_when_the_service_has_a_file(tmp_path):
    daq, sim, _ = _started()
    daq.config_path = str(tmp_path / "usb6001.ini")
    daq.cfg.dio.lines[9].direction = "out"
    daq.apply_config()
    back = Config.load(daq.config_path)
    assert back.dio.lines[9].direction == "out"
    daq.shutdown()


def test_concurrent_setters_and_polling_do_not_collide():
    daq, sim, _ = _started(poll=True)
    errors = []

    def hammer():
        try:
            for k in range(50):
                daq.set_ao(0, (k % 10) * 0.1)
                daq.status()
        except Exception as exc:                         # pragma: no cover
            errors.append(exc)
    threads = [threading.Thread(target=hammer) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    daq.shutdown()
    assert errors == []
