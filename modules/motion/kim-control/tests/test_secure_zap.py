"""The key checker (secure.ZapHandler) -- no asyncio, no tornado, no hang.

Found on the lab PC (2026-09-30, Windows, Python 3.14): pyzmq's
ThreadAuthenticator runs an asyncio loop, which on Windows needs `tornado`
to watch zmq sockets. Without it the thread died at start: no encrypted
client was ever answered, and stopping the service waited forever (a test run
hung for hours). secure.py now answers libzmq's ZAP requests itself.
"""

from __future__ import annotations

import time

import pytest

zmq = pytest.importorskip("zmq")

from kim import secure  # noqa: E402


class FakeGuard:
    def __init__(self, allow=True, boom=False):
        self.allow, self.boom, self.asked = allow, boom, []

    def callback(self, domain, key):
        self.asked.append((domain, key))
        if self.boom:
            raise RuntimeError("broken check")
        return self.allow


def _req(domain=b"d1", mechanism=b"CURVE", key=b"\x01" * 32):
    return [b"1.0", b"42", domain, b"127.0.0.1", b"", mechanism, key]


def test_it_answers_curve_by_the_guard_and_says_who_it_was():
    h = secure.ZapHandler(zmq.Context.instance())
    h.providers["d1"] = FakeGuard(allow=True)
    h.providers["d2"] = FakeGuard(allow=False)
    h.providers["d3"] = FakeGuard(boom=True)
    ok = h._answer(_req(b"d1"))
    assert ok[:4] == [b"1.0", b"42", b"200", b"OK"]
    from zmq.utils import z85
    assert ok[4] == z85.encode(b"\x01" * 32)              # User-Id = the key, z85
    assert h._answer(_req(b"d2"))[2] == b"400"
    assert h._answer(_req(b"d3"))[2] == b"400"            # a crashing check lets no one in
    assert h._answer(_req(b"nobody"))[2] == b"400"        # CURVE on an unknown domain
    assert h._answer(_req(mechanism=b"NULL", key=None)[:6])[2] == b"200"   # a plain socket
    assert h._answer([b"1.0"])[2] == b"500"


def test_it_starts_stops_quickly_and_refuses_to_start_twice():
    ctx = zmq.Context()
    try:
        a = secure.ZapHandler(ctx)
        a.start()
        assert a.alive()
        b = secure.ZapHandler(ctx)
        with pytest.raises(secure.SecurityError, match="did not start"):
            b.start()                                     # the ZAP address is taken
        t0 = time.monotonic()
        a.stop()
        assert time.monotonic() - t0 < 2.5 and not a.alive()
    finally:
        ctx.term()


def test_a_real_curve_handshake_is_decided_by_it():
    """End to end on one context: a known key gets an answer, a stranger none."""
    ctx = zmq.Context()
    srv_pub, srv_sec = zmq.curve_keypair()
    good_pub, good_sec = zmq.curve_keypair()
    bad_pub, bad_sec = zmq.curve_keypair()
    h = secure.ZapHandler(ctx)
    h.start()

    class G:
        def callback(self, domain, key):
            return key == good_pub
    h.providers["test"] = G()
    rep = ctx.socket(zmq.REP)
    rep.zap_domain, rep.curve_server = b"test", True
    rep.curve_secretkey, rep.curve_publickey = srv_sec, srv_pub
    port = rep.bind_to_random_port("tcp://127.0.0.1")

    def ask(pub, sec):
        s = ctx.socket(zmq.REQ)
        s.linger, s.rcvtimeo = 0, 800
        s.curve_publickey, s.curve_secretkey, s.curve_serverkey = pub, sec, srv_pub
        s.connect(f"tcp://127.0.0.1:{port}")
        s.send(b"hi")
        try:
            return s.recv()
        except zmq.Again:
            return None
        finally:
            s.close(0)
    try:
        import threading

        def serve():
            if rep.poll(3000):
                f = rep.recv(copy=False)
                rep.send(f.get("User-Id").encode())
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        assert ask(good_pub, good_sec) == good_pub         # answered, and knows who
        t.join(2)
        assert ask(bad_pub, bad_sec) is None               # a stranger: no answer
    finally:
        rep.close(0)
        h.stop()
        ctx.term()
