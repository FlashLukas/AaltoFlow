"""The camera drives piezo (XY) and zpiezo (Z focus) as a MACHINE client.

backends/remote_xy.py and backends/remote_z.py talk to the piezo-control and
zpiezo-control services. Those services now have control (one controller, many
viewers -- control.py): while a person's GUI on ANOTHER PC holds control, only
that PC and "machine" clients may change anything. The camera's stabiliser,
click-to-go and autofocus must keep working then (Lukas, 2026-09-29, the same
rule as camera -> kim), so both links name themselves kind "machine", name
"camera", in every command.

The stand-in services below run the SAME gate code the real piezo / zpiezo
services run (control.py is a byte-identical copy in every module), with a GUI
at "user@other-pc" holding control. Ports 18610 (XY) and 18611 (Z).
"""

from __future__ import annotations

import threading

import pytest

zmq = pytest.importorskip("zmq")

XY_CMD, Z_CMD = 18610, 18611


class _GatedService:
    """A REP socket behind a ControlLease: a GUI on another PC holds control.

    Answers what the camera's links send (status, info, move_xy, set_voltage)
    once the gate lets the request through, and keeps every request it got."""

    def __init__(self, port: int, safety=("stop",)):
        from camera.control import ControlLease, make_identity
        self.gate = ControlLease(safety=set(safety))
        person = make_identity("gui", "piezo GUI")
        person["host"] = "user@other-pc"           # NOT this PC
        assert self.gate.handle({"cmd": "take_control", "client": person})["granted"]
        self.requests: list[dict] = []
        self.applied: list[dict] = []              # commands that got past the gate
        self._rep = zmq.Context.instance().socket(zmq.REP)
        self._rep.setsockopt(zmq.LINGER, 0)
        self._rep.bind(f"tcp://127.0.0.1:{port}")
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        poller = zmq.Poller()
        poller.register(self._rep, zmq.POLLIN)
        while not self._stop.is_set():
            if dict(poller.poll(50)):
                req = self._rep.recv_json()
                self.requests.append(req)
                gate = self.gate.handle(req)
                if gate is not None:
                    self._rep.send_json(gate)
                    continue
                self.applied.append(req)
                cmd = req.get("cmd")
                if cmd == "info":
                    reply = {"ok": True, "info": {"limits": {"v_min": 0.0, "v_max": 75.0}}}
                elif cmd == "status":
                    reply = {"ok": True, "status": {"position": [1.0, 2.0],
                                                    "moving": [False, False],
                                                    "voltage": 12.5, "hw_error": ""}}
                else:
                    reply = {"ok": True}
                self._rep.send_json(reply)

    def close(self):
        self._stop.set()
        self._t.join(1.0)
        self._rep.close(0)


def _raw(port: int, req: dict) -> dict:
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.RCVTIMEO, 2000)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(f"tcp://127.0.0.1:{port}")
    try:
        s.send_json(req)
        return s.recv_json()
    finally:
        s.close(0)


def test_the_xy_link_to_piezo_passes_while_a_gui_on_another_pc_holds_control():
    from camera.backends.remote_xy import RemoteXYStage
    svc = _GatedService(XY_CMD)
    xy = RemoteXYStage("127.0.0.1", XY_CMD, timeout_ms=1500)
    xy.open()
    try:
        assert xy.identity["kind"] == "machine" and xy.identity["name"] == "camera"
        xy.move_xy(10.0, 20.0)                        # the stabiliser goes on
        assert xy.read_xy() == (1.0, 2.0)
        assert xy.moving() is False
        moves = [r for r in svc.applied if r.get("cmd") == "move_xy"]
        assert moves and moves[0]["x"] == 10.0
        # every command named the camera as a machine
        assert all(r.get("client", {}).get("kind") == "machine" for r in svc.requests)
        # the same move WITHOUT the identity is refused (the GUI holds control)
        r = _raw(XY_CMD, {"cmd": "move_xy", "x": 1.0, "y": 1.0})
        assert r["ok"] is False and r["refused"] == "control"
    finally:
        xy.close()
        svc.close()


def test_the_z_link_to_zpiezo_passes_while_a_gui_on_another_pc_holds_control():
    from camera.backends.remote_z import RemoteZFocus
    svc = _GatedService(Z_CMD, safety=())             # zpiezo has no safety verb
    z = RemoteZFocus("127.0.0.1", Z_CMD, timeout_ms=1500)
    z.open()
    try:
        assert z.identity["kind"] == "machine" and z.identity["name"] == "camera"
        assert z.z_range() == (0.0, 75.0)
        z.set_z(30.0)                                 # autofocus goes on
        assert z.read_z() == 12.5
        sets = [r for r in svc.applied if r.get("cmd") == "set_voltage"]
        assert sets and sets[0]["volts"] == 30.0
        assert all(r.get("client", {}).get("kind") == "machine" for r in svc.requests)
        r = _raw(Z_CMD, {"cmd": "set_voltage", "volts": 5.0})
        assert r["ok"] is False and r["refused"] == "control"
    finally:
        z.close()
        svc.close()
