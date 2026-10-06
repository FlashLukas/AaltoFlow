"""shutdown{keep_outputs} (Lukas, 2026-10-06).

A plain `shutdown` stays the SAFE stop: ramp the coils to 0 V, enable off.
With `keep_outputs: true` it is a RESTART for a code update: the loop stops
and the DAQ is closed and released, but nothing is written -- the coils keep
their drive and the next start adopts it.
"""

import threading
import time

import pytest

pytest.importorskip("zmq")

from mag2dcal.config import Config
from mag2dcal.net.service import Mag2dcalService
from mag2dcal.sim_system import build_sim_system

CMD, PUB = 26130, 26131          # a port block no other mag2dcal test uses


def _wait(pred, timeout_s=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pred():
            return True
        time.sleep(0.03)
    return False


def _energized_service():
    cfg = Config()
    ctrl, sim = build_sim_system(cfg, seed=4)
    svc = Mag2dcalService(ctrl, host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    svc.start()
    ctrl.set_output(True)
    ctrl.set_field(20.0, 30.0)
    assert _wait(lambda: ctrl.status().field_stable)
    # Log every output write made by THIS (the test's) thread: svc.stop() runs
    # the shutdown here, while the control loop's own last tick runs on its
    # thread -- only the former is the shutdown path under test.
    sim.main_writes = []
    me = threading.current_thread()
    for name in ("write_ao", "set_enable"):
        real = getattr(sim, name)

        def spy(*a, _real=real, _name=name):
            if threading.current_thread() is me:
                sim.main_writes.append(_name)
            return _real(*a)
        setattr(sim, name, spy)
    return svc, sim


def test_keep_outputs_leaves_the_coils_driven():
    svc, sim = _energized_service()
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is True
    svc.stop()
    assert sim.main_writes == []                 # no AO / enable write at all
    assert sim.enable is True and max(abs(v) for v in sim.ao) > 0.1
    assert sim.is_open is False                  # but closed all the same


def test_plain_shutdown_still_ramps_to_zero():
    svc, sim = _energized_service()
    r = svc._dispatch({"cmd": "shutdown"})
    assert r["kept_outputs"] is False
    svc.stop()
    assert "write_ao" in sim.main_writes         # the ramp down
    assert sim.ao == [0.0, 0.0] and sim.enable is False


def test_keep_outputs_text_false_is_false():
    # gotcha #3: the string "false" from a hand-typed console must not keep
    svc, sim = _energized_service()
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": "false"})
    assert r["kept_outputs"] is False
    svc.stop()
    assert sim.ao == [0.0, 0.0] and sim.enable is False


def test_a_restart_during_a_calibration_still_ramps_down():
    """A calibration is a half-done sweep (the coils at up to v_max), not a
    state anyone chose: a restart then stops as safely as Stop does."""
    cfg = Config()
    ctrl, sim = build_sim_system(cfg, seed=4)
    svc = Mag2dcalService(ctrl, host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    svc.start()
    events = []
    ctrl._on_event = lambda level, msg: events.append((level, msg))
    ctrl.set_output(True)
    ctrl.calibrate(n_per_leg=5, dwell_s=0.2, v_max=2.0)
    assert _wait(lambda: ctrl.status().state == "CALIBRATE")
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"]
    svc.stop()
    assert sim.ao == [0.0, 0.0] and sim.enable is False
    assert any("during a calibration" in m for _l, m in events)
