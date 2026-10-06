"""shutdown{keep_outputs} (Lukas, 2026-10-06).

A plain `shutdown` switches the tracking generator OFF. With
`keep_outputs: true` (a restart for a code update) it is left on.
Either way the instrument is closed and released.
"""

import time

import pytest

pytest.importorskip("zmq")

from gsp818.config import Config
from gsp818.net.service import Gsp818Service
from gsp818.sim_system import build_sim_system

CMD, PUB = 26240, 26241          # a port block no other gsp818 test uses
# backend calls that CHANGE the instrument (reads are not logged)
WRITES = ("set_", "setup_", "start_zero", "cancel_zero")
OUTPUTS = []         # what a plain stop sends to make outputs safe


def _service():
    brain, sim = build_sim_system(Config(), realtime=False, seed=4, state={"tg_on": True})
    svc = Gsp818Service(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    svc.start()
    time.sleep(0.3)
    # from here on, log every state-changing call the backend receives
    sim.calls = []
    for name in dir(type(sim)):
        if name.startswith(WRITES):
            real = getattr(sim, name)

            def spy(*a, _real=real, _name=name, **kw):
                sim.calls.append(_name)
                return _real(*a, **kw)
            setattr(sim, name, spy)
    return svc, sim


def test_keep_outputs_changes_no_output():
    svc, sim = _service()
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is True
    svc.stop()
    assert sim.calls == []                       # nothing sent that changes it
    assert sim.tg_output is True and ("tg_on", False) not in sim.writes
    assert sim._open is False                    # but closed all the same


def test_plain_shutdown_unchanged():
    svc, sim = _service()
    r = svc._dispatch({"cmd": "shutdown"})
    assert r["ok"] and r["kept_outputs"] is False
    svc.stop()
    assert sim.tg_output is False              # TG switched off
    assert sim._open is False


def test_keep_outputs_text_false_is_false():
    # gotcha #3: the string "false" from a hand-typed console must not keep
    svc, sim = _service()
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": "false"})
    assert svc._keep_outputs is False and r["kept_outputs"] is False
    svc.stop()
    assert sim.tg_output is False              # TG switched off
