"""shutdown{keep_outputs} -- a RESTART for a code update (Lukas, 2026-10-06).

Plain `shutdown` stays as it was. `shutdown{keep_outputs: true}` still closes
the instrument (and so releases it) and still stops motion (a SAFETY step a restart keeps: no move may run on
unsupervised),
but changes nothing else the instrument holds: no move back, no home, no
park -- the next start ADOPTS the state. The text "false" must be False
(gotcha #3). Ports 17166/17167 belong to this file only.
"""

import time

import pytest
import zmq

from agilis.config import Config
from agilis.net.service import AgilisService
from agilis.sim_system import build_sim_system

CMD, PUB = 17166, 17167

# Method names that would CHANGE the instrument (motion, voltages, modes).
WRITES = ("move", "home", "set_", "jog", "zero", "goto", "park", "step",
          "enable", "disable")


def _record(obj, log):
    """Wrap every public method of a sim backend so each call is logged by name."""
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(obj, name)
        except Exception:
            continue
        if not callable(attr) or isinstance(attr, type):
            continue

        def wrap(*a, _n=name, _f=attr, **k):
            log.append(_n)
            return _f(*a, **k)

        try:
            setattr(obj, name, wrap)
        except Exception:
            pass


def _closed(be) -> bool:
    return getattr(be, "_opened", getattr(be, "_open", None)) is False


def _send_shutdown(**args) -> dict:
    """The raw wire, as the launcher sends it."""
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.connect(f"tcp://127.0.0.1:{CMD}")
    try:
        s.send_json({"cmd": "shutdown", **args})
        return s.recv_json()
    finally:
        s.close()


@pytest.fixture()
def started():
    cfg = Config()
    cfg.hardware.poll_hz = 50
    brain, be = build_sim_system(cfg)
    backends = [be]
    svc = AgilisService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    time.sleep(0.2)
    log: list[str] = []
    for be in backends:
        _record(be, log)
    try:
        yield svc, brain, backends, log
    finally:
        svc.stop()                      # idempotent: a no-op after the test's own stop
        time.sleep(0.1)


def _shutdown(started, **args):
    svc, _brain, _backends, log = started
    reply = _send_shutdown(**args)
    svc.stop()                          # what serve_forever's finally does
    return reply, log


@pytest.mark.parametrize("args, kept", [
    ({}, False),
    ({"keep_outputs": True}, True),
    ({"keep_outputs": "true"}, True),
    ({"keep_outputs": "false"}, False),     # gotcha #3: not bool("false")
    ({"keep_outputs": False}, False),
])
def test_reply_says_what_was_kept(started, args, kept):
    reply, _ = _shutdown(started, **args)
    assert reply["ok"] is True and reply["stopping"] is True
    assert reply["kept_outputs"] is kept


def test_keep_outputs_changes_nothing_but_still_closes(started):
    _svc, brain, backends, _ = started
    reply, log = _shutdown(started, keep_outputs=True)
    assert reply["kept_outputs"] is True
    assert [n for n in log if n.startswith(WRITES)] == []
    assert "stop" in log          # motion stopped: the safety step is kept
    assert all(_closed(be) for be in backends)        # released all the same


def test_plain_shutdown_unchanged(started):
    _svc, brain, backends, _ = started
    reply, log = _shutdown(started)
    assert reply["kept_outputs"] is False
    assert "stop" in log
    assert all(_closed(be) for be in backends)
