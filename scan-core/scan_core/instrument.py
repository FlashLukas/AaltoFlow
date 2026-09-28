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


class InstrumentFault(InstrumentError):
    """The service answers, but its status says its readings are not trustworthy.

    Raised by `wait_until` when the status keeps carrying a non-empty
    `hw_error` (the last hardware read failed) or `fault` (the module itself
    says measuring now would give wrong data -- the camera lost its pattern).
    A settle wait must not declare a knob "arrived" on such a frame: the flag
    it would believe was computed from a read that failed. The engine treats
    this like any other fault: it pauses (GUI) or stops (headless).
    """

    #: how the engine (which cannot import pyzmq) recognises one
    is_fault = True

    def __init__(self, message: str, instrument: str = ""):
        super().__init__(message)
        self.instrument = instrument


#: Seconds a PUB status frame may be old before `status()` stops trusting it.
#: Services publish at 5-10 Hz, so 2 s is 10-20 missed frames: not a hiccup.
STALE_AFTER_S = 2.0

#: Seconds a settle wait tolerates `hw_error` / `fault` before it raises. One
#: failed read in a long magnet ramp should not end a point; a failure that
#: persists must. (The flag is never BELIEVED during that time -- only the
#: decision to give up waits.)
FAULT_GRACE_S = 1.0


def status_problem(st: dict) -> str:
    """The reason this status frame cannot be trusted, or "" when it can.

    Suite-wide convention (docs/DEVELOPER_NOTES.md section 4): `hw_error` is
    "" when fine and the error text when the last hardware read failed;
    `fault` is "" when fine and a message when the module says a measurement
    now would be wrong. A missing key means "fine" -- older modules do not
    publish them. Anything falsy (None, False, "") is fine.
    """
    if not isinstance(st, dict):
        return ""
    hw = st.get("hw_error")
    fault = st.get("fault")
    parts = []
    if fault:
        parts.append("fault" if fault is True else str(fault))
    if hw:
        parts.append("hardware read failed: " + ("?" if hw is True else str(hw)))
    return "; ".join(parts)


class Instrument:
    """A live connection to one instrument service.

    Holds a REQ socket for commands (guarded by a lock, since REQ is strictly
    one request at a time) and a background SUB thread that caches the latest
    status frame -- so polling `status()` costs nothing on the network.
    """

    def __init__(self, name: str, host: str = "localhost",
                 cmd_port: int = 5555, pub_port: int | None = None,
                 timeout_ms: int = 3000, stale_after_s: float = STALE_AFTER_S,
                 fault_grace_s: float = FAULT_GRACE_S):
        self.name = name
        #: see STALE_AFTER_S / FAULT_GRACE_S; per instrument, so a test (or a
        #: service that publishes unusually slowly) can change them
        self.stale_after_s = float(stale_after_s)
        self.fault_grace_s = float(fault_grace_s)
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
        #: time.monotonic() of the last PUB status frame (None = none yet)
        self._latest_t: float | None = None
        #: malformed frames seen, and when one was last reported (rate limit)
        self.bad_frames = 0
        self._bad_logged_t = -1e9
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
        """Latest status as a plain dict, from the PUB cache where possible.

        The cache is only trusted while it is FRESH (younger than
        `stale_after_s`). Before 2026-09-28 it was trusted forever: a service
        that crashed kept "answering" with its last frame -- stable flags,
        readings and all -- and a detector reading the status recorded that
        frozen number at every later point, with nothing raised. Now an old
        cache falls back to asking the service directly, and if that fails too
        this RAISES, naming how long the service has been silent (Lukas's
        decision A: a dead instrument must be loud).
        """
        with self._lock:
            d = dict(self._latest)
            t = self._latest_t
        age = None if t is None else time.monotonic() - t
        if d and age is not None and age <= self.stale_after_s:
            return d
        try:
            return self.command("status").get("status", {})
        except InstrumentError as exc:
            if age is None:
                raise                           # never heard from it at all
            raise InstrumentError(
                f"{self.name}: the service has not published a status for "
                f"{age:.1f} s and does not answer a status request either "
                f"({exc}). Is it still running?") from exc

    def latest(self) -> dict | None:
        """The cached status if it is FRESH, else None -- never the network.

        For a GUI timer (the control panel polls every instrument a few times
        a second on the GUI thread): `status()` falls back to a request, which
        for a dead service blocks for the whole REQ timeout -- the window would
        freeze. A panel would rather show nothing new than a frozen window or
        a stale number presented as live.
        """
        with self._lock:
            d = dict(self._latest)
            t = self._latest_t
        if d and t is not None and time.monotonic() - t <= self.stale_after_s:
            return d
        return None

    def status_age(self) -> float | None:
        """Seconds since the last PUB status frame; None if none arrived yet."""
        with self._lock:
            t = self._latest_t
        return None if t is None else time.monotonic() - t

    def wait_until(self, predicate, timeout_s: float = 30.0,
                   poll_s: float = 0.05, what: str = "condition",
                   cancel=None) -> dict:
        """Block until `predicate(status_dict)` holds; return that status.

        Raises TimeoutError rather than returning a flag. A scan that quietly
        records points the hardware never reached produces data that looks
        perfectly fine and is wrong, so this failure has to be loud.

        `should_abort` (set by `Lab.set_abort`) is checked every poll, so the
        operator's Abort takes effect DURING a settle wait. Without it, Abort is
        only seen between points, and a knob that never settles makes the whole
        window look frozen for the length of the timeout.

        A frame carrying `hw_error` or `fault` (see `status_problem`) is NEVER
        accepted as settled: its done-flag was computed from a read that
        failed. If the problem lasts longer than `fault_grace_s` this raises
        InstrumentFault, which the engine turns into a PAUSE (or a clear stop
        when nobody is there to fix it) -- instead of silently accepting the
        point, as it did before 2026-09-28.

        `cancel()` -> True ends the wait at once, returning the current status:
        the knob has been SUPERSEDED by a newer command (see the manifest
        setter). A target-echo settle (gotcha #40) would otherwise wait for an
        echo of a target that no longer exists -- a fly row ended by a "stop
        here" setpoint left its own move waiting for up to its whole timeout.
        """
        deadline = time.monotonic() + timeout_s
        bad_since = None
        while True:
            st = self.status()
            if cancel is not None and cancel():
                return st
            problem = status_problem(st)
            if problem:
                now = time.monotonic()
                bad_since = now if bad_since is None else bad_since
                if now - bad_since >= self.fault_grace_s:
                    raise InstrumentFault(
                        f"{self.name}: {problem} (while waiting for {what})",
                        instrument=(getattr(self, "alias", None)
                                    or (getattr(self, "manifest", None) or {}).get("module")
                                    or self.name))
            else:
                bad_since = None
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
        """The SUB thread: keep the latest status frame and its arrival time.

        It must SURVIVE a bad frame. Before 2026-09-28 one malformed message
        (a module mid-restart, a stray publisher on the port, a non-JSON
        payload) killed this thread silently -- and the cache then held that
        instrument's last status forever. Now a bad frame is counted, reported
        through `on_event` (at most every 10 s, or a broken publisher at 10 Hz
        would flood the log), and skipped.
        """
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        while not self._stop.is_set():
            try:
                if not poller.poll(200):
                    continue
                parts = self._sub.recv_multipart()
            except zmq.ZMQError:
                if self._stop.is_set():
                    break                       # closing: the socket went away
                time.sleep(0.2)
                continue
            try:
                if len(parts) != 2:
                    raise ValueError(f"expected [topic, json], got {len(parts)} part(s)")
                topic, payload = parts
                d = json.loads(payload)
                if not isinstance(d, dict):
                    raise ValueError(f"payload is a {type(d).__name__}, not an object")
                if topic == b"status":
                    with self._lock:
                        self._latest = d
                        self._latest_t = time.monotonic()
                elif topic == b"event":
                    self._emit(d.get("level", "info"), d.get("msg", ""))
            except Exception as exc:
                self.bad_frames += 1
                now = time.monotonic()
                if now - self._bad_logged_t >= 10.0:
                    self._bad_logged_t = now
                    self._emit("warn", f"{self.name}: ignored a malformed "
                                       f"message ({exc}); {self.bad_frames} so far")

    def _emit(self, level, msg):
        """Call on_event without letting a broken callback kill the listener."""
        try:
            self.on_event(level, msg)
        except Exception:
            pass


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

    Both keys may be PATHS (["target_um", 0], ["moving", 0]) for a multi-axis
    service that publishes per-axis lists -- the TARGET ECHO rule of the motion
    modules (docs/DEVELOPER_NOTES.md gotcha #40). `tol` is the manifest's `tol`:
    a module that stores the target it actually commanded (a stepper rounding
    um to whole steps) declares how far that may be from the requested value.
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
