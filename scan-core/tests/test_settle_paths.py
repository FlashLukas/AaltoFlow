"""Settle keys that point INTO lists, and acquisitions keyed on the trigger reply.

Two things the lock-in module (hf2) needed, and one bug they flushed out:

  * per-channel values travel as lists (`tc_set_s: [ch1, ch2]`), so a settle
    block names `key` plus `index`
  * kim ALREADY declared `{"key": "moving", "index": i}` -- and the index was
    ignored. bool([False, False, False]) is True, so a kim position sweep could
    never settle. `test_kim_shaped_moving_flag_settles_per_axis` is that bug.
  * a lock-in acquisition must not be satisfied by the previous acquisition's
    "not acquiring" -- `target_key` makes the trigger's reply the wait target.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

zmq = pytest.importorskip("zmq")

from scan_core.instrument import Instrument
from scan_core.manifest import register_manifest, resolve_settle
from scan_core.registry import Registry


# ---- index / path keys -----------------------------------------------------------

def test_kim_shaped_moving_flag_settles_per_axis():
    make = resolve_settle({"policy": "flag_only", "key": "moving",
                           "invert": True, "index": 1})
    settled = make(None)
    assert settled({"moving": [True, False, True]}) is True     # Y stopped
    assert settled({"moving": [False, True, False]}) is False   # Y moving


def test_echoes_on_a_list_entry():
    settled = resolve_settle({"policy": "echoes", "key": "tc_set_s",
                              "index": 1, "tol": 1e-12})(0.03)
    assert settled({"tc_set_s": [0.01, 0.03]}) is True
    assert settled({"tc_set_s": [0.03, 0.01]}) is False


def test_a_list_without_an_index_is_an_error_not_a_silent_hang():
    settled = resolve_settle({"policy": "flag_only", "key": "moving",
                              "invert": True})(None)
    with pytest.raises(TypeError, match="index"):
        settled({"moving": [False, False, False]})


def test_missing_index_entry_is_not_settled():
    settled = resolve_settle({"policy": "echoes", "key": "tc_set_s",
                              "index": 5})(0.01)
    assert settled({"tc_set_s": [0.01, 0.01]}) is False


# ---- a lock-in with a stale window after `acquire` --------------------------------

class FakeLockIn:
    """Speaks the suite contract; `acquire` is fire-and-forget with a STALE window.

    After `acquire` replies with a new id, the status keeps describing the
    previous acquisition (old id, acquiring=False, old sample) for `stale_s`,
    then acquires for `busy_s`, then latches a sample equal to the id.
    """

    def __init__(self, cmd_port, stale_s=0.3, busy_s=0.3, target_key=True):
        self.cmd_port, self.pub_port = cmd_port, cmd_port + 1
        self.stale_s, self.busy_s = stale_s, busy_s
        self._lock = threading.Lock()
        self._next = 0
        self._shown_id = 0
        self._acquiring = False
        self._sample = 0.0
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()
        acquire = {"group": "sample", "trigger_verb": "acquire",
                   "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                             "flag_key": "acquiring", "invert": True}}
        if target_key:
            acquire["target_key"] = "acq_id"
        else:
            acquire["ready"] = {"policy": "flag_only", "key": "acquiring", "invert": True}
        self.manifest = {"schema": 1, "module": "lockin", "revision": 1, "parameters": [
            {"id": "r", "label": "R", "kind": "indicator", "type": "float",
             "unit": "V", "read_path": ["sample", "r", 0], "acquire": acquire}]}

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
                    "sample": {"r": [self._sample, 0.0]}}

    def _run(self, n):
        time.sleep(self.stale_s)
        with self._lock:
            self._shown_id, self._acquiring = n, True
        time.sleep(self.busy_s)
        with self._lock:
            self._sample, self._acquiring = float(n), False

    def _serve(self):
        rep = self._ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        poller = zmq.Poller(); poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(100):
                msg = rep.recv_json()
                cmd = msg.get("cmd")
                if cmd == "acquire":
                    with self._lock:
                        self._next += 1
                        n = self._next
                    threading.Thread(target=self._run, args=(n,), daemon=True).start()
                    rep.send_json({"ok": True, "acq_id": n})
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


def _measure_twice(port, target_key):
    svc = FakeLockIn(port, target_key=target_key).start()
    inst = Instrument("lockin", host="127.0.0.1", cmd_port=port)
    try:
        time.sleep(0.2)                      # a status frame in the cache
        reg = Registry()
        register_manifest(reg, inst, inst.command("describe")["describe"])
        det = reg.get("r")
        values = []
        for _ in range(2):
            det.acquire.trigger()
            det.acquire.wait()
            values.append(det.get())
        return values
    finally:
        inst.close()
        svc.stop()


def test_target_key_waits_for_this_acquisition():
    assert _measure_twice(15960, target_key=True) == [1.0, 2.0]


def test_without_target_key_the_read_is_one_acquisition_behind():
    """Executable documentation: `flag_only` on `acquiring` returns at once on
    the stale frame, so each point reads the PREVIOUS acquisition. Nothing
    raises; the numbers are just wrong."""
    values = _measure_twice(15962, target_key=False)
    assert values != [1.0, 2.0]
    assert values[0] == 0.0                  # the sample that existed before
