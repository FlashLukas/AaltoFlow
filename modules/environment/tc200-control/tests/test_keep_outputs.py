"""shutdown{keep_outputs} (Lukas, 2026-10-06).

A plain `shutdown` stays as it was: the heater is switched OFF
(hardware.disable_on_shutdown). With `keep_outputs: true` it is a RESTART for
a code update: the port is closed and released, but the heater is left as it
is and the next start adopts it.
"""

import time

import pytest

pytest.importorskip("zmq")

from tc200.config import Config
from tc200.net.service import Tc200Service
from tc200.sim_system import build_sim_system

CMD, PUB = 26150, 26151          # a port block no other tc200 test uses
WRITES = ("set_", "toggle_")     # every backend call that changes the box


def _service():
    cfg = Config()
    cfg.hardware.poll_s = 0.05
    heater, sim = build_sim_system(cfg, temperature_C=30.0, setpoint_C=30.0,
                                   enabled=True, seed=2)
    svc = Tc200Service(heater, host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    svc.start()
    time.sleep(0.2)
    # log every state-changing call the backend receives from here on
    sim.writes = []
    for name in dir(type(sim)):
        if name.startswith(WRITES):
            real = getattr(sim, name)

            def spy(*a, _real=real, _name=name, **kw):
                sim.writes.append(_name)
                return _real(*a, **kw)
            setattr(sim, name, spy)
    return svc, sim


def test_keep_outputs_leaves_the_heater_on():
    svc, sim = _service()
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is True
    svc.stop()
    assert sim.writes == []                      # no command to the box
    assert sim.enabled is True
    assert sim._open is False                    # but closed all the same


def test_plain_shutdown_still_switches_the_heater_off():
    svc, sim = _service()
    r = svc._dispatch({"cmd": "shutdown"})
    assert r["kept_outputs"] is False
    svc.stop()
    assert "toggle_enable" in sim.writes and sim.enabled is False


def test_keep_outputs_text_false_is_false():
    # gotcha #3: the string "false" from a hand-typed console must not keep
    svc, sim = _service()
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": "false"})
    assert r["kept_outputs"] is False
    svc.stop()
    assert sim.enabled is False
