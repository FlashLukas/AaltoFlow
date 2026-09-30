"""The launcher's probes (describe, shutdown) reach a module the lab's policy
secures over CurveZMQ (suite_common/secure.py), and "Add a service on another
PC" -- which does not know the module yet -- still finds it.

A stand-in service with the real guard answers on port 18966.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

zmq = pytest.importorskip("zmq")
pytest.importorskip("PySide6")

from suite_common import secure  # noqa: E402

PORT = 18966
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # mission_control.py


@pytest.fixture
def secured_kim(tmp_path, monkeypatch):
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

    ctx = zmq.Context.instance()
    rep = ctx.socket(zmq.REP)
    rep.setsockopt(zmq.LINGER, 0)
    guard = secure.secure_server(ctx, [rep], "kim")
    rep.bind(f"tcp://127.0.0.1:{PORT}")
    stop = threading.Event()

    def serve():
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not stop.is_set():
            if poller.poll(50):
                msg = rep.recv_json()
                if msg.get("cmd") == "describe":
                    rep.send_json({"ok": True, "describe": {"module": "kim", "revision": 1,
                                                            "parameters": []}})
                else:
                    rep.send_json({"ok": True})
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield
    stop.set()
    t.join(timeout=2)
    rep.close(0)
    secure.release_server(guard)
    time.sleep(0.1)


def test_describe_and_shutdown_reach_a_secured_module(secured_kim):
    import mission_control as MC
    assert MC.fetch_describe("127.0.0.1", PORT, module="kim")["module"] == "kim"
    assert MC.fetch_describe("127.0.0.1", PORT, module="kim@pc-a:5567")["module"] == "kim"
    assert MC.request_shutdown("127.0.0.1", PORT, module="kim")


def test_add_a_remote_service_finds_a_secured_module_it_does_not_know(secured_kim):
    import mission_control as MC
    # plain first (no answer from a CurveZMQ server), then the secured modules' way
    assert MC.fetch_describe("127.0.0.1", PORT, timeout_ms=500)["module"] == "kim"


def test_a_module_the_policy_does_not_secure_is_asked_plain(secured_kim):
    import mission_control as MC
    assert MC.fetch_describe("127.0.0.1", PORT, timeout_ms=500, module="piezo") is None
