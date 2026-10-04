"""scan-core's generic client speaks CurveZMQ to exactly the modules the lab's
policy secures (suite_common/secure.py), and plain to the rest.

No service is needed: the socket's security mechanism is set before it
connects. Port 18965 is only named, never reached.
"""

from __future__ import annotations

import json

import pytest

zmq = pytest.importorskip("zmq")

from suite_common import secure  # noqa: E402

from scan_core.instrument import Instrument  # noqa: E402


@pytest.fixture
def secured(tmp_path, monkeypatch):
    me, kr = tmp_path / "me", tmp_path / "keyring"
    me.mkdir()
    kr.mkdir()
    public, secret = secure.new_keypair()
    secure.write_cert(me / secure.OWN_PUBLIC, public, meta={"pc": "pc-a"})
    secure.write_cert(me / secure.OWN_SECRET, public, secret, meta={"pc": "pc-a"})
    (me / secure.SETTINGS_FILE).write_text(json.dumps({"keyring": str(kr)}), encoding="utf-8")
    (kr / secure.POLICY_FILE).write_text(json.dumps({"mode": "enforce", "modules": ["kim"]}),
                                         encoding="utf-8")
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(me))
    monkeypatch.setattr(secure, "_flipped", {})       # no mode remembered from another test
    return public


@pytest.mark.parametrize("name, curve", [("kim", True), ("kim_pc-a", True),
                                         ("piezo", False)])
def test_the_instrument_uses_curve_only_where_the_policy_says(secured, name, curve):
    inst = Instrument(name, host="127.0.0.1", cmd_port=18965, timeout_ms=200)
    try:
        want = zmq.CURVE if curve else zmq.NULL
        assert inst._req.getsockopt(zmq.MECHANISM) == want
        assert inst._sub.getsockopt(zmq.MECHANISM) == want
        inst._reset_req()                      # the rebuilt socket, too
        assert inst._req.getsockopt(zmq.MECHANISM) == want
    finally:
        inst.close()


def test_a_secured_module_on_an_unknown_pc_says_what_is_missing(secured):
    with pytest.raises(secure.SecurityError, match="no key for '10.9.9.9'"):
        Instrument("kim", host="10.9.9.9", cmd_port=18965, timeout_ms=200)


def test_a_scan_reaches_a_service_after_the_policy_was_switched_off(secured, tmp_path):
    """Lab PC, 2026-10-03: the policy went "off" while kim ran encrypted.
    A scan built afterwards speaks plain first, hears nothing, and then
    (secure.no_answer) talks to kim the way it really runs; the status
    stream follows."""
    import threading
    import time
    ctx = zmq.Context.instance()
    rep, pub = ctx.socket(zmq.REP), ctx.socket(zmq.PUB)
    for s in (rep, pub):
        s.setsockopt(zmq.LINGER, 0)
    guard = secure.secure_server(ctx, [rep, pub], "kim")      # encrypted (enforce)
    rep.bind("tcp://127.0.0.1:18967")
    pub.bind("tcp://127.0.0.1:18968")
    stop = threading.Event()

    def serve():
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not stop.is_set():
            if poller.poll(50):
                rep.recv_json()
                rep.send_json({"ok": True, "status": {"x": 1}})
            pub.send_multipart([b"status", json.dumps({"x": 2}).encode()])
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    (tmp_path / "keyring" / secure.POLICY_FILE).write_text(
        json.dumps({"mode": "off", "modules": ["kim"]}), encoding="utf-8")
    inst = Instrument("kim", host="127.0.0.1", cmd_port=18967, timeout_ms=500)
    try:
        assert inst._req.getsockopt(zmq.MECHANISM) == zmq.NULL     # the policy's way
        assert inst.command("status")["ok"]                        # then the service's
        assert inst._req.getsockopt(zmq.MECHANISM) == zmq.CURVE
        t0 = time.monotonic()
        while inst.latest() is None and time.monotonic() - t0 < 3:
            time.sleep(0.05)
        assert inst.latest() == {"x": 2}
    finally:
        inst.close()
        stop.set()
        t.join(timeout=2)
        rep.close(0)
        pub.close(0)
        secure.release_server(guard)
