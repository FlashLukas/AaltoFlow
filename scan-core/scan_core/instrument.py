"""instrument.py -- one ZeroMQ client that works for EVERY service in the suite.

The whole suite speaks one wire contract (see `docs/DEVELOPER_NOTES.md` section 4):

    commands   REQ/REP, JSON; every reply is {"ok": true, ...} or
               {"ok": false, "error": ...}
    telemetry  PUB/SUB, multipart [topic, json], topics b"status" and b"event"
    universal  every service answers status / info / get_config / set_config
    semantics  FIRE-AND-FORGET -- {"ok": true} means ACCEPTED, not DONE

Because that contract is uniform, scan-core does not need seven client classes
and does not import a single instrument package. It needs one generic client
plus, per knob, a short declaration of three things: which verb sets it, which
status field reads it back, and how you can tell it has arrived.

That last one is where naive code goes wrong -- see `adopt_then_flag`.

Keeping this generic is deliberate: scan-core knows the PROTOCOL, not the
instruments. Adding an instrument to a scan is then a declaration in `lab.py`,
not new code here.
"""

from __future__ import annotations

import json
import threading
import time

import zmq


#: Defined in errors.py (which imports nothing) so the engine can catch it
#: without importing pyzmq; re-exported here because this is where callers
#: have always imported it from.
from .errors import ScanAborted  # noqa: E402,F401


class InstrumentError(RuntimeError):
    """A service replied {"ok": false}, or could not be reached at all."""


class Instrument:
    """A live connection to one instrument service.

    Holds a REQ socket for commands (guarded by a lock, since REQ is strictly
    one request at a time) and a background SUB thread that caches the latest
    status frame -- so polling `status()` costs nothing on the network.
    """

    def __init__(self, name: str, host: str = "localhost",
                 cmd_port: int = 5555, pub_port: int | None = None,
                 timeout_ms: int = 3000):
        self.name = name
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port if pub_port is not None else cmd_port + 1

        self._ctx = zmq.Context.instance()
        self._timeout_ms = timeout_ms
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.connect(f"tcp://{host}:{self.cmd_port}")

        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(f"tcp://{host}:{self.pub_port}")
        self._sub.setsockopt(zmq.SUBSCRIBE, b"")

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.on_event = lambda level, msg: None
        #: set by Lab.set_abort: checked inside every settle wait, so Abort does
        #: not have to wait for the timeout of a knob that will never settle
        self.should_abort = None

        self._t = threading.Thread(target=self._listen,
                                   name=f"scan-sub-{name}", daemon=True)
        self._t.start()

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> dict:
        """Confirm the service is really there; return its `info` block.

        Call this before a scan starts. Failing here costs a second; failing on
        the first setpoint costs however long it took to prepare the sample.
        """
        return self.command("info").get("info", {})

    def close(self) -> None:
        self._stop.set()
        self._t.join(timeout=1.0)
        self._req.close(0)
        self._sub.close(0)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- commands and status --------------------------------------------

    def command(self, verb: str, _timeout_ms: int | None = None, **kwargs) -> dict:
        """Send one command. Raises InstrumentError unless the reply says ok.

        `_timeout_ms` raises the receive timeout for this ONE call. Needed when
        a module chooses to block inside the verb rather than the suite's usual
        trigger-and-poll: a VNA sweep can take many seconds, and the default 3 s
        would time out, rebuild the socket and fail the scan. Prefer a module
        that returns immediately and reports readiness in status -- but a
        blocking read is sometimes all a driver gives you.
        """
        msg = {"cmd": verb}
        msg.update(kwargs)
        with self._req_lock:
            if _timeout_ms is not None:
                self._req.setsockopt(zmq.RCVTIMEO, int(_timeout_ms))
            try:
                self._req.send_json(msg)
                reply = self._req.recv_json()
            except zmq.Again:
                # A timed-out REQ socket is stuck in the wrong half of its
                # state machine and will never work again -- rebuild it, or
                # every later command in the scan fails too.
                self._reset_req()
                waited = _timeout_ms if _timeout_ms is not None else self._timeout_ms
                raise InstrumentError(
                    f"{self.name}: no reply to {verb!r} within "
                    f"{waited} ms (is the service running on "
                    f"{self.host}:{self.cmd_port}?)")
            finally:
                if _timeout_ms is not None:
                    # Always put the default back, even on the error path, or
                    # one slow read silently changes the timeout of every later
                    # command on this socket.
                    self._req.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        if not reply.get("ok"):
            raise InstrumentError(
                f"{self.name}: {verb} refused: "
                f"{reply.get('error', 'no reason given')}")
        return reply

    def status(self) -> dict:
        """Latest status as a plain dict, from the PUB cache where possible."""
        with self._lock:
            d = dict(self._latest)
        if d:
            return d
        return self.command("status").get("status", {})   # no frame yet

    def wait_until(self, predicate, timeout_s: float = 30.0,
                   poll_s: float = 0.05, what: str = "condition") -> dict:
        """Block until `predicate(status_dict)` holds; return that status.

        Raises TimeoutError rather than returning a flag. A scan that quietly
        records points the hardware never reached produces data that looks
        perfectly fine and is wrong, so this failure has to be loud.

        `should_abort` (set by `Lab.set_abort`) is checked every poll, so the
        operator's Abort takes effect DURING a settle wait. Without it, Abort is
        only seen between points, and a knob that never settles makes the whole
        window look frozen for the length of the timeout.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            st = self.status()
            if predicate(st):
                return st
            if self.should_abort is not None and self.should_abort():
                raise ScanAborted(f"{self.name}: aborted while waiting for {what}")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"{self.name}: waited {timeout_s:g} s for {what}; "
                    f"last status={_brief(st)}")
            time.sleep(poll_s)

    # ---- internals -------------------------------------------------------

    def _reset_req(self):
        endpoint = self._req.LAST_ENDPOINT
        self._req.close(0)
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        if endpoint:
            self._req.connect(
                endpoint.decode() if isinstance(endpoint, bytes) else endpoint)

    def _listen(self):
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(200):
                topic, payload = self._sub.recv_multipart()
                d = json.loads(payload)
                if topic == b"status":
                    with self._lock:
                        self._latest = d
                elif topic == b"event":
                    self.on_event(d.get("level", "info"), d.get("msg", ""))


def _brief(st: dict, keys: int = 6) -> str:
    """A short one-line status digest, for timeout messages."""
    items = list(st.items())[:keys]
    return "{" + ", ".join(f"{k}={v!r}" for k, v in items) + "}"


# --------------------------- settle policies --------------------------------
#
# "The command was accepted" and "the hardware got there" are different events,
# and the gap between them is where wrong data comes from. A settle policy is a
# factory: give it the target value, get back a predicate over a status dict.
#
# A `key` is either a top-level status field name, or a PATH (a list of keys
# and indices) for per-axis / per-channel values that a service publishes as
# lists: ["moving", 0] is axis X of kim, ["tc_set_s", 1] channel 2 of hf2.

def _lookup(st: dict, key):
    """Resolve a status key or key path; None if any step is missing."""
    if not isinstance(key, (list, tuple)):
        return st.get(key)
    cur = st
    for k in key:
        if isinstance(k, int) and isinstance(cur, (list, tuple)):
            if not -len(cur) <= k < len(cur):
                return None
            cur = cur[k]
        elif isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def _scalar(value, key):
    """Refuse a list or dict where a policy needs ONE value, loudly.

    The trap this closes: a per-axis flag published as a list is truthy even
    when every entry is False -- bool([False, False, False]) is True -- so a
    `moving` policy without an index never settles and a scan times out with
    no hint why. Better to say exactly that.
    """
    if isinstance(value, (list, tuple, dict)):
        raise TypeError(
            f"settle key {key!r} is a {type(value).__name__} in the status, "
            f"not a single value; the manifest must name an `index` for it")
    return value

def adopt_then_flag(setpoint_key: str, flag_key: str, invert: bool = False,
                    tol: float = 1e-6):
    """Settle predicate: adopt our setpoint FIRST, then trust the done-flag.

    The trap this avoids: commands are fire-and-forget, so for the first few
    polls after a set, the service is still describing the PREVIOUS point. If
    that point had settled, its done-flag is still True. Watching the flag alone
    therefore returns immediately, at the old value, and the scan records a
    whole grid measured one step behind. It looks like clean data.

    So the predicate requires two things in order: the service has ADOPTED our
    target (`status[setpoint_key]` matches), and only then is `status[flag_key]`
    believed.

    `invert=True` for services whose flag means "busy" rather than "done" (a
    stage's `moving`), which is how the set-and-forget half of the suite reports.
    """
    def make(target: float):
        def settled(st: dict) -> bool:
            sp = _scalar(_lookup(st, setpoint_key), setpoint_key)
            if sp is None or abs(float(sp) - float(target)) > tol:
                return False              # not adopted yet -> status is stale
            flag = bool(_scalar(_lookup(st, flag_key), flag_key))
            return (not flag) if invert else flag
        return settled
    return make


def echoes(status_key: str, tol: float = 1e-6):
    """Settle predicate: wait until the service echoes our value back.

    This is the right policy for the set-and-forget half of the suite (an RF
    generator, a piezo voltage). Those instruments have no "settled" flag,
    because there is nothing to converge -- but their status does report the
    value they are currently holding. Waiting for that echo confirms the command
    was not just accepted but applied, which is strictly better than sleeping
    and hoping.
    """
    def make(target: float):
        def settled(st: dict) -> bool:
            v = _scalar(_lookup(st, status_key), status_key)
            return v is not None and abs(float(v) - float(target)) <= tol
        return settled
    return make


def flag_only(flag_key: str, invert: bool = False):
    """Settle predicate for knobs whose setpoint is not echoed in status.

    Weaker than `adopt_then_flag`: it cannot distinguish a stale status from a
    fresh one, so use it only where the service offers no setpoint to check.
    """
    def make(target: float):
        def settled(st: dict) -> bool:
            flag = bool(_scalar(_lookup(st, flag_key), flag_key))
            return (not flag) if invert else flag
        return settled
    return make


def immediate():
    """Settle predicate for genuinely set-and-forget knobs (e.g. RF power).

    Some instruments really do arrive as fast as they can be commanded. Saying
    so explicitly beats a sleep, and keeps the declaration honest about which
    knobs have never been verified to settle.
    """
    def make(target: float):
        return lambda st: True
    return make
