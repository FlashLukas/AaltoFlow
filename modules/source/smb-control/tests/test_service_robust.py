"""Two service bugs from the 2026-09-28 deep cleaning (docs/DEVELOPER_NOTES.md
gotcha #39), checked offline on non-default ports 15692..15695.

(A) A port that is already taken must make start() FAIL -- before the
    instrument is opened -- instead of leaving a deaf process that holds it.
(B) A malformed request must be ANSWERED: a REP socket that received and sent
    nothing back refuses every later request, so one bad message used to take
    the whole command port down.
"""

import pytest
import zmq

from smb.config import Config
from smb.net.service import SmbService, PortInUse
from smb.sim_system import build_sim_system

CMD, PUB = 15692, 15693


def _brain():
    brain, *_ = build_sim_system(Config())
    return brain


def test_taken_port_fails_start_before_the_instrument_opens():
    blocker = zmq.Context.instance().socket(zmq.REP)
    blocker.bind(f"tcp://127.0.0.1:{CMD + 2}")
    brain = _brain()
    started = []
    brain.start = lambda *a, **k: started.append(True)   # must never be reached
    try:
        svc = SmbService(brain, host="127.0.0.1", cmd_port=CMD + 2, pub_port=PUB + 2)
        with pytest.raises(PortInUse):
            svc.start()
        assert not started, "the instrument was opened although the port was taken"
    finally:
        blocker.close(0)


def test_malformed_request_is_answered_and_the_port_survives():
    svc = SmbService(_brain(), host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    svc.start()
    ctx = zmq.Context.instance()
    try:
        for payload in (b"this is not JSON {", b"[1, 2, 3]"):
            s = ctx.socket(zmq.REQ)
            s.setsockopt(zmq.LINGER, 0)
            s.setsockopt(zmq.RCVTIMEO, 3000)
            s.connect(f"tcp://127.0.0.1:{CMD}")
            s.send(payload)
            reply = s.recv_json()          # zmq.Again here = the old bug
            s.close(0)
            assert reply["ok"] is False and reply.get("error")
        s = ctx.socket(zmq.REQ)
        s.setsockopt(zmq.LINGER, 0)
        s.setsockopt(zmq.RCVTIMEO, 3000)
        s.connect(f"tcp://127.0.0.1:{CMD}")
        s.send_json({"cmd": "status"})
        assert s.recv_json()["ok"] is True
        s.close(0)
    finally:
        svc.stop()
