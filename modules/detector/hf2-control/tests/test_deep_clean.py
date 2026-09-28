"""Regression tests from the deep-cleaning pass of 2026-09-28.

Each test here FAILED on the code as it was and pins one bug that was fixed.
Network tests use non-default ports (15894..15899) so they never collide with a
running service or with test_net.py.
"""

import importlib.util
import io
import os
import time
from contextlib import redirect_stdout

import pytest
import zmq

from hf2.config import Config
from hf2.sim_system import build_sim_system
from hf2.net.service import Hf2Service
from hf2.net.client import Hf2Client

from test_lockin import FakeClock


# ---- 1. a reading taken BEFORE the trigger must never be latched ------------------

def test_acquire_does_not_latch_a_reading_taken_before_the_trigger():
    """The poll thread reads the demodulators, and only THEN looks at the
    clock to decide whether the acquisition has settled. If `acquire()` lands
    while that read is in flight (a USB read takes milliseconds) and the settle
    time is short, the reading that was taken BEFORE the trigger -- i.e. of the
    previous scan point -- used to be latched as the settled sample."""
    clock = FakeClock()
    li, sim = build_sim_system(Config(), clock=clock, seed=1)
    sim.noise_V_rtHz = 0.0
    sim.drift = 0.0
    li.start(poll=False)
    try:
        for ch in (1, 2):                   # settle = 4.6 x 10 us = 46 us
            li.set_time_constant(ch, 1e-5)
            li.set_order(ch, 1)
        clock.advance(0.01, li, polls=10)
        old_r = li.status().live["r"][0]

        orig = sim.read_demods
        fired = []

        def read_then_trigger(demods):
            out = orig(demods)              # the sample is taken HERE ...
            if not fired:
                fired.append(True)
                sim.set_signal(0, 5e-3, 0.0)   # ... then the experiment moves on,
                li.acquire()                   # the scan triggers,
                clock.t += 1e-3                # and the USB read finishes 1 ms later
            return out

        sim.read_demods = read_then_trigger
        li.poll_once()
        for _ in range(50):
            if not li.status().acquiring:
                break
            clock.advance(1e-3, li)
        r = li.get_sample()["r"][0]
        assert abs(old_r - 2e-3) < 1e-4      # the old point really was 2 mV
        assert r == pytest.approx(5e-3, rel=0.02), \
            f"latched {r:.4g} V: the reading from before the trigger"
    finally:
        li.shutdown()


# ---- 2. the polling thread must survive anything, not only a failed read ----------

def test_poll_thread_survives_an_error_after_the_read():
    li, sim = build_sim_system(Config(), seed=1)
    li.start()
    try:
        time.sleep(0.1)
        good = sim.read_aux
        sim.read_aux = lambda: [0.1]          # a malformed reply: one value, not two
        time.sleep(0.2)
        assert li._thread.is_alive(), "the polling thread died -- status would freeze"
        assert li.status().hw_error, "the failure must show as hw_error"
        sim.read_aux = good
        deadline = time.monotonic() + 2.0
        while li.status().hw_error and time.monotonic() < deadline:
            time.sleep(0.02)
        assert li.status().hw_error == ""     # and it recovers on its own
    finally:
        li.shutdown()


# ---- 3. a refused set_config must reach the caller and change nothing -------------

CMD3, PUB3 = 15894, 15895


@pytest.fixture
def svc_cli():
    li, sim = build_sim_system(Config(), seed=3)
    svc = Hf2Service(li, host="127.0.0.1", cmd_port=CMD3, pub_port=PUB3, status_hz=20.0)
    svc.start()
    cli = Hf2Client(host="127.0.0.1", cmd_port=CMD3, pub_port=PUB3, timeout_ms=2000)
    time.sleep(0.2)
    yield svc, cli
    cli.shutdown()
    svc.stop()


def test_remote_apply_config_reports_a_refusal(svc_cli):
    """The GUI's Settings > Apply catches ValueError. The local LockIn raises
    one; the remote client used to return None and drop the service's
    {"ok": false}, so a refused Apply looked like it had worked."""
    svc, cli = svc_cli
    cli.get_config()
    cli.cfg.ch1.reference = "sideways"
    with pytest.raises(ValueError):
        cli.apply_config()


def test_refused_set_config_leaves_the_live_config_alone(svc_cli):
    """set_config wrote every value into the live cfg BEFORE checking them, so
    a refused request still left 'sideways' behind as channel 1's reference --
    reported in status and describe although the instrument never saw it."""
    svc, cli = svc_cli
    r = cli._cmd({"cmd": "set_config",
                  "config": {"ch1": {"reference": "sideways", "time_constant_s": 0.5}}})
    assert r["ok"] is False
    st = svc.lockin.status()
    assert st.reference[0] == "internal"
    assert st.tc_set_s[0] == Config().ch1.time_constant_s


# ---- 4. the Settings panel must show a hardware refusal, not swallow it -----------

def test_settings_panel_reports_a_backend_error(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from hf2.apps.settings_dialog import SettingsPanel

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    li, sim = build_sim_system(Config(), seed=1)
    li.start(poll=False)
    try:
        panel = SettingsPanel(li, li.cfg, on_applied=lambda: None)

        def refuse(*a, **k):
            raise RuntimeError("ZIAPINotFoundException: /dev0000/demods/0/rate")

        monkeypatch.setattr(sim, "setup_channel", refuse)
        assert panel.apply() is False
        assert "ZIAPINotFound" in panel.error.text()
    finally:
        li.shutdown()


# ---- 5. a port that is taken must stop the service, not leave a zombie ------------

def test_service_start_fails_cleanly_when_the_port_is_taken():
    """The sockets were bound inside the daemon threads: a taken port killed
    the thread with a traceback nobody sees, while the process carried on --
    holding the lock-in (and its hwlock claim) with no way to command it."""
    ctx = zmq.Context.instance()
    squatter = ctx.socket(zmq.REP)
    squatter.bind("tcp://127.0.0.1:15896")
    li, _ = build_sim_system(Config(), seed=1)
    svc = Hf2Service(li, host="127.0.0.1", cmd_port=15896, pub_port=15897)
    try:
        with pytest.raises(zmq.ZMQError):
            svc.start()
        assert not li.status().connected, "the instrument was opened anyway"
        # and the pub port was released again, so a retry can bind it
        probe = ctx.socket(zmq.PUB)
        probe.bind("tcp://127.0.0.1:15897")
        probe.close(0)
    finally:
        squatter.close(0)
        li.shutdown()


# ---- 6. the console must print a sample with a switched-off channel ---------------

def _load_console():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "hf2_console.py")
    spec = importlib.util.spec_from_file_location("hf2_console_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_console_acquire_survives_a_null_channel():
    """A switched-off demodulator's values travel as null. The console's
    `acquire` formatted theta with '+7.2f' and died with a TypeError."""
    mod = _load_console()
    con = mod.Console("127.0.0.1", 15898, 15899)
    sample = {"acq_id": 1, "settle_s": 0.1, "n_avg": 1,
              "x": [1e-3, None], "y": [0.0, None], "r": [1e-3, None],
              "theta_deg": [0.0, None], "aux_in": [0.1, None]}
    replies = {"acquire": {"ok": True, "acq_id": 1},
               "get_config": {"ok": True, "config": {"acquisition": {"timeout_s": 1}}},
               "status": {"ok": True, "status": {"acq_id": 1, "acquiring": False,
                                                  "sample": sample}}}
    con.send = lambda msg: replies[msg["cmd"]]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            con.acquire()
    finally:
        con.close()
    assert "ch2" in buf.getvalue()
