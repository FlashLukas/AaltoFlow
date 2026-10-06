"""shutdown{keep_outputs} (Lukas, 2026-10-06).

This shutdown never changes an output (the source is the analyser's own
sweep), so the flag changes nothing: both stops send no output command.
Either way the instrument is closed and released.
"""

import time

import pytest

pytest.importorskip("zmq")

from vna.config import Config
from vna.net.service import VnaService
from vna.sim_system import build_sim_system

CMD, PUB = 26170, 26171          # a port block no other vna test uses
# backend calls that CHANGE the instrument (reads are not logged)
WRITES = ("set_", "setup_", "start_zero", "cancel_zero")
OUTPUTS = []         # what a plain stop sends to make outputs safe


def _service():
    brain, sim = build_sim_system(Config(), realtime=False, seed=4)
    svc = VnaService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
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
    assert sim._open is False                    # but closed all the same


def test_plain_shutdown_unchanged():
    svc, sim = _service()
    r = svc._dispatch({"cmd": "shutdown"})
    assert r["ok"] and r["kept_outputs"] is True
    svc.stop()
    assert sim.calls == []                       # nothing to make safe
    assert sim._open is False


def test_keep_outputs_text_false_is_false():
    # gotcha #3: the string "false" from a hand-typed console must not keep
    svc, sim = _service()
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": "false"})
    assert svc._keep_outputs is False and r["kept_outputs"] is True
    svc.stop()
    assert sim.calls == []                       # nothing to make safe
