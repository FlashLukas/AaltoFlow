"""Actions from `describe`: which become registry Actions, and how they WAIT.

A module offers an action to scans by giving it a `wait` block (contract,
2026-09-16). The VNA's `take_reference` is the case:

    "wait": {"target_key": "acq_id",
             "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                       "flag_key": "acquiring", "invert": true},
             "timeout_s": 30}

The fake below has the same stale window as FakeLockIn in test_settle_paths:
after `take_reference` replies with a new id, the status keeps describing the
PREVIOUS acquisition ("not acquiring") for a moment. A wait that does not target
THIS id returns on that stale frame -- and the scan would then divide by a
reference that does not exist yet.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

zmq = pytest.importorskip("zmq")

from scan_core import Recipe, run                                    # noqa: E402
from scan_core.errors import ScanAborted                             # noqa: E402
from scan_core.instrument import Instrument                          # noqa: E402
from scan_core.manifest import register_manifest                     # noqa: E402
from scan_core.registry import Gettable, Registry                    # noqa: E402


class FakeReferenceVNA:
    """`take_reference` is fire-and-forget: stale for `stale_s`, busy for
    `busy_s`, then the reference id is latched. `busy_s=None` never finishes."""

    def __init__(self, cmd_port, stale_s=0.3, busy_s=0.3, target_key=True):
        self.cmd_port, self.pub_port = cmd_port, cmd_port + 1
        self.stale_s, self.busy_s = stale_s, busy_s
        self._lock = threading.Lock()
        self._next = 0
        self._shown_id = 0
        self._acquiring = False
        self._reference_id = 0
        self.commands: list[dict] = []
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()
        wait = {"ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                          "flag_key": "acquiring", "invert": True},
                "timeout_s": 5}
        if target_key:
            wait["target_key"] = "acq_id"
        else:
            wait["ready"] = {"policy": "flag_only", "key": "acquiring", "invert": True}
        self.manifest = {"schema": 1, "module": "vna", "revision": 1, "parameters": [
            {"id": "take_reference", "label": "Take reference", "kind": "action",
             "type": "action", "wait": wait,
             "args": [{"name": "averages", "type": "int", "default": 4}]},
            {"id": "clear_reference", "label": "Clear reference", "kind": "action",
             "type": "action", "wait": {"ready": {"policy": "immediate"}}},
            {"id": "preset", "label": "Preset", "kind": "action", "type": "action",
             "danger": True},                               # no wait: panel only
            {"id": "reference_id", "label": "Reference", "kind": "indicator",
             "type": "int", "read_path": ["reference_id"]},
        ]}

    def start(self):
        for fn in (self._serve, self._publish):
            threading.Thread(target=fn, daemon=True).start()
        time.sleep(0.15)
        return self

    def stop(self):
        self._stop.set()
        time.sleep(0.2)

    def _status(self):
        with self._lock:
            return {"acq_id": self._shown_id, "acquiring": self._acquiring,
                    "reference_id": self._reference_id}

    def _run(self, n):
        time.sleep(self.stale_s)
        with self._lock:
            self._shown_id, self._acquiring = n, True
        if self.busy_s is None:
            return                                   # a reference that never ends
        time.sleep(self.busy_s)
        with self._lock:                             # one critical section (gotcha 28)
            self._reference_id, self._acquiring = n, False

    def _serve(self):
        rep = self._ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        poller = zmq.Poller(); poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(100):
                msg = rep.recv_json()
                self.commands.append(msg)
                cmd = msg.get("cmd")
                if cmd == "take_reference":
                    with self._lock:
                        self._next += 1
                        n = self._next
                    threading.Thread(target=self._run, args=(n,), daemon=True).start()
                    rep.send_json({"ok": True, "acq_id": n})
                elif cmd == "clear_reference":
                    with self._lock:
                        self._reference_id = 0
                    rep.send_json({"ok": True})
                elif cmd == "status":
                    rep.send_json({"ok": True, "status": self._status()})
                elif cmd == "describe":
                    rep.send_json({"ok": True, "describe": self.manifest})
                else:
                    rep.send_json({"ok": False, "error": f"unknown {cmd}"})
        rep.close(0)

    def _publish(self):
        pub = self._ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{self.pub_port}")
        while not self._stop.is_set():
            pub.send_multipart([b"status", json.dumps(self._status()).encode()])
            time.sleep(0.03)
        pub.close(0)


@pytest.fixture
def vna_on():
    made = []

    def make(port, **kw):
        svc = FakeReferenceVNA(port, **kw).start()
        inst = Instrument("vna", host="127.0.0.1", cmd_port=port)
        made.append((svc, inst))
        time.sleep(0.2)                      # a status frame in the cache
        reg = Registry()
        added = register_manifest(reg, inst, inst.command("describe")["describe"],
                                  prefix=True)
        return svc, inst, reg, added

    yield make
    for svc, inst in made:
        inst.close()
        svc.stop()


def test_only_actions_with_a_wait_block_are_registered(vna_on):
    _, _, reg, added = vna_on(15904)
    assert [a.id for a in reg.actions()] == ["vna.take_reference", "vna.clear_reference"]
    assert "vna.preset" not in added and reg.get_action("vna.preset") is None
    assert reg.get("vna.take_reference") is None          # not a Parameter
    assert reg.get_action("vna.take_reference").label == "Take reference"


def test_the_action_sends_the_default_args(vna_on):
    svc, _, reg, _ = vna_on(15906)
    reg.get_action("vna.take_reference").run()
    sent = [m for m in svc.commands if m.get("cmd") == "take_reference"]
    assert sent == [{"cmd": "take_reference", "averages": 4}]


def test_the_action_waits_for_ITS_acquisition(vna_on):
    """Run twice: each run returns only once THAT reference is latched."""
    svc, inst, reg, _ = vna_on(15908)
    act = reg.get_action("vna.take_reference")
    for n in (1, 2):
        act.run()
        assert inst.status()["reference_id"] == n


def test_without_target_key_the_action_returns_on_the_stale_frame(vna_on):
    """Executable documentation of the trap the target_key closes."""
    svc, inst, reg, _ = vna_on(15910, target_key=False)
    reg.get_action("vna.take_reference").run()
    assert inst.status()["reference_id"] == 0            # nothing latched yet


def test_an_immediate_action_returns_at_once(vna_on):
    _, _, reg, _ = vna_on(15912)
    t = time.monotonic()
    reg.get_action("vna.clear_reference").run()
    assert time.monotonic() - t < 1.0


def test_abort_interrupts_a_long_action_wait_and_the_scan_ends_as_aborted(vna_on):
    """A reference that never finishes must not make Abort look dead: the wait
    raises ScanAborted, the engine runs after_scan, and the abort reaches the
    caller as an abort (ScanWorker reports "aborted", not a failure)."""
    svc, inst, reg, _ = vna_on(15914, busy_s=None)
    after = []
    reg.add(Gettable("dummy", "dummy", "", lambda: 0.0))
    from scan_core.registry import Action
    reg.add_action(Action("mark", "mark", lambda: after.append("after_scan ran")))
    pressed = {"at": time.monotonic() + 0.8}
    inst.should_abort = lambda: time.monotonic() >= pressed["at"]
    recipe = Recipe(axes=[], detectors=["dummy"],
                    hooks=[{"when": "before_scan", "action": "call",
                            "args": {"action": "vna.take_reference"}},
                           {"when": "after_scan", "action": "call",
                            "args": {"action": "mark"}}])
    t = time.monotonic()
    with pytest.raises(ScanAborted):
        run(recipe, reg, created_iso="t")
    assert time.monotonic() - t < 3.0                    # not the 5 s timeout
    assert after == ["after_scan ran"]
