"""Who may CHANGE an instrument: one controller, everyone else a viewer.

Many clients can connect to one service at the same time: GUIs on different
PCs, scan-core, the camera driving kim, a script, a console. Before this file,
every one of them could change everything, and nobody could see who else was
connected. Lukas (2026-09-29), for the lab where several people train on one
instrument: the first GUI gets control, every later one opens as a VIEWER, and
control changes hands only by a deliberate act.

The rules (docs/DEVELOPER_NOTES.md, "Control: one controller, many viewers"):

* A client names itself in every request: ``"client": {"id", "kind",
  "name", "host"}``. ``kind`` is "gui", "script" or "machine".
* The SERVICE enforces it, not the GUI -- a greyed-out button protects nothing
  against a script or an older GUI. A command that changes something is
  refused unless it comes from the holder of control.
* Always allowed: read verbs (status, info, describe, get_* / read_* /
  list_*), the control verbs themselves, the SAFETY verbs a module declares
  (stop, kill_af, abort...) -- a viewer watching a stage run away must be able
  to stop it -- and ``shutdown`` (the launcher's clean stop, gotcha #25: a
  refused shutdown would end in a hard kill).
* ``kind == "machine"`` bypasses the lock (Lukas's choice): the camera
  moving kim during an autofocus, scan-core during a scan. Opening a kim GUI
  must not break a running autofocus. It is SELF-declared: this is a guard
  against mistakes between people who follow the rules, not security (anyone
  who reaches the port can send anything; the firewall is the security).
* Control belongs to a PC, not to one window: every client on the holder's
  PC may change things (Lukas: the kim GUI and the measurement suite on the
  lab PC both drive kim; the trainee sits at a DIFFERENT PC). See same_pc().
* A machine that changed something in the last DRIVING_S is marked
  ``driving`` in the status's client list, and every control bar says "also
  driving: scan-core" -- a stage moving under a person's GUI is never a
  mystery.
* Nobody holds control -> everything is allowed, with or without an id, as
  before this file existed (a headless setup with no GUI works unchanged).
* ``take_control{force: false}`` takes control only when it is free;
  ``force: true`` takes it over (the GUI asks the user to confirm first); the
  old holder becomes a viewer and sees who took it. A script is a client like
  a GUI: it must take control explicitly.
* A holder that goes silent for ``lease_s`` (a crashed GUI, a script that
  ended without releasing) loses control, so nothing stays locked forever.
  Clients send ``heartbeat`` every ``HEARTBEAT_S``.

This file is stdlib only. Every module carries its OWN byte-identical copy
(``src/<pkg>/control.py``, like hwlock.py); ``tools/check_modules.py`` checks
the copies against this master (suite-common/src/suite_common/control.py).
"""

from __future__ import annotations

import getpass
import socket
import threading
import time
import uuid

#: How often a client says "still here" (s). The lease below is five of these.
HEARTBEAT_S = 2.0
#: A holder not heard from for this long loses control (s).
LEASE_S = 10.0
#: Viewers not heard from for this long drop off the published client list.
CLIENT_FORGET_S = 3 * LEASE_S

#: Verbs every module treats as read-only (plus the prefixes below).
READ_VERBS = frozenset({"status", "info", "describe", "get_config",
                        "heartbeat", "take_control", "release_control",
                        "clients"})
READ_PREFIXES = ("get_", "read_", "list_")
#: Verbs allowed for everyone, always. ``shutdown``: see the module docstring.
ALWAYS_VERBS = frozenset({"shutdown"})

KINDS = ("gui", "script", "machine")


def make_identity(kind: str = "script", name: str = "") -> dict:
    """A fresh identity for one client object: random id + who and where.

    The user and PC names are read at run time and travel only to the
    service (never into a tracked file). They are what another viewer sees:
    "control: kim GUI (anna@lab-pc)".
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    try:
        user = getpass.getuser()
    except Exception:
        user = "?"
    return {"id": uuid.uuid4().hex, "kind": kind, "name": name or kind,
            "host": f"{user}@{socket.gethostname()}"}


def pc_of(ident: dict | None) -> str:
    """The PC a client runs on ("user@PC" -> "pc"), "" when unknown."""
    host = str((ident or {}).get("host") or "")
    return host.rpartition("@")[2].strip().lower()


def same_pc(a: dict | None, b: dict | None) -> bool:
    """Control belongs to a PC, not to one window (Lukas, 2026-09-29: "if it is
    the same machine you can leave kim unlocked"; the trainee sits at a
    DIFFERENT PC). Two windows on the lab PC -- the kim GUI and the measurement
    suite, say -- both control kim; a GUI on another PC is a viewer until it
    takes control. An unknown PC never matches."""
    pa, pb = pc_of(a), pc_of(b)
    return bool(pa) and pa == pb


#: A machine client that changed something within this many seconds is shown
#: as "also driving" in every control bar, so a stage moving under a person's
#: GUI (a scan, the camera's autofocus) is never a mystery.
DRIVING_S = 10.0


def describe_holder(h: dict | None) -> str:
    """One line for a GUI or an error text: 'kim GUI (anna@lab-pc)'."""
    if not h:
        return "nobody"
    return f"{h.get('name') or h.get('kind') or 'a client'} ({h.get('host') or '?'})"


class ControlLease:
    """The service side: who holds control, who is watching, and the gate.

    ``safety`` = the module's verbs a viewer may always send (stop, kill_af,
    abort...); ``read`` = extra read-only verbs whose names do not start with
    get_/read_/list_ (e.g. ``stream_read``). ``on_event(level, msg)`` reports
    hand-overs into the service's event stream, so every GUI log shows them.
    ``clock`` is injectable for tests.
    """

    def __init__(self, safety=(), read=(), lease_s: float = LEASE_S,
                 on_event=None, clock=time.monotonic):
        self.safety = frozenset(safety)
        self.read = frozenset(read)
        self.lease_s = float(lease_s)
        self.on_event = on_event or (lambda level, msg: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._holder: dict | None = None       # identity + "since" (wall clock)
        self._holder_seen = 0.0                # monotonic
        self._clients: dict[str, tuple[dict, float]] = {}   # id -> (identity, seen)
        self._changed: dict[str, float] = {}   # machine id -> when it last changed something

    # ------------------------------------------------------------------ #
    def is_write(self, cmd) -> bool:
        """True when ``cmd`` may change something (so it needs control)."""
        if not isinstance(cmd, str):
            return False                       # the service answers "unknown command"
        if cmd in READ_VERBS or cmd in ALWAYS_VERBS or cmd in self.read or cmd in self.safety:
            return False
        return not cmd.startswith(READ_PREFIXES)

    @staticmethod
    def _identity(req: dict) -> dict | None:
        c = req.get("client") if isinstance(req, dict) else None
        if not isinstance(c, dict) or not c.get("id"):
            return None
        return {"id": str(c["id"]), "kind": str(c.get("kind") or "script"),
                "name": str(c.get("name") or ""), "host": str(c.get("host") or "")}

    def _expire(self, now: float) -> None:
        # caller holds the lock
        if self._holder is not None and now - self._holder_seen > self.lease_s:
            old = self._holder
            self._holder = None
            self.on_event("warn", f"control: {describe_holder(old)} went silent for "
                                  f"{self.lease_s:.0f} s -- control is free")
        for cid in [k for k, (_, seen) in self._clients.items()
                    if now - seen > CLIENT_FORGET_S]:
            del self._clients[cid]
            self._changed.pop(cid, None)

    def _seen(self, ident: dict | None, now: float) -> None:
        # caller holds the lock. Any client on the holder's PC keeps the
        # lease alive: control belongs to the PC (same_pc), so closing one of
        # its two windows must not free the instrument.
        if ident is None:
            return
        self._clients[ident["id"]] = (ident, now)
        if self._holder is not None and same_pc(self._holder, ident):
            self._holder_seen = now

    def _holds(self, ident: dict | None) -> bool:
        # caller holds the lock
        return ident is not None and self._holder is not None and (
            ident["id"] == self._holder["id"] or same_pc(ident, self._holder))

    # ------------------------------------------------------------------ #
    def handle(self, req: dict) -> dict | None:
        """Run the gate and the control verbs for one request.

        Returns a REPLY when this file answers the request itself (a control
        verb, or a refusal); None when the service should dispatch it as usual.
        Call it first thing in ``_dispatch``.
        """
        cmd = req.get("cmd") if isinstance(req, dict) else None
        ident = self._identity(req)
        now = self._clock()
        with self._lock:
            self._expire(now)
            self._seen(ident, now)

            if cmd == "heartbeat":
                return {"ok": True, "control": self._status_locked(now)}
            if cmd == "clients":
                return {"ok": True, "control": self._status_locked(now)}
            if cmd == "take_control":
                return self._take(ident, bool(req.get("force", False)), now)
            if cmd == "release_control":
                if self._holds(ident):
                    self._holder = None
                    self.on_event("info", f"control: released by {describe_holder(ident)}")
                    return {"ok": True, "released": True, "control": self._status_locked(now)}
                return {"ok": True, "released": False, "control": self._status_locked(now)}

            if not self.is_write(cmd):
                return None
            if ident is not None and ident["kind"] == "machine":
                self._changed[ident["id"]] = now      # shown as "also driving"
                return None
            if self._holder is None:
                return None
            if self._holds(ident):
                return None
            since = time.strftime("%H:%M", time.localtime(self._holder.get("since", 0)))
            who = describe_holder(self._holder)
            hint = ("take control first (client.take_control(force=True), or the "
                    "Take control button in a GUI)")
            if ident is None:
                hint = ("this request did not say who sent it; " + hint)
            return {"ok": False, "refused": "control",
                    "error": f"read-only: {who} has control of this instrument "
                             f"(since {since}); {cmd!r} was not sent -- {hint}",
                    "control": self._status_locked(now)}

    def _take(self, ident: dict | None, force: bool, now: float) -> dict:
        # caller holds the lock
        if ident is None:
            return {"ok": False, "error": "take_control needs a client identity "
                                          "(\"client\": {\"id\", \"kind\", \"name\", \"host\"})"}
        h = self._holder
        if self._holds(ident):                 # ours, or our PC's: nothing to take
            return {"ok": True, "granted": True, "control": self._status_locked(now)}
        if h is not None and not force:
            return {"ok": True, "granted": False, "control": self._status_locked(now)}
        self._holder = dict(ident, since=time.time())
        self._holder_seen = now
        if h is not None:
            self.on_event("warn", f"control: {describe_holder(ident)} took over from "
                                  f"{describe_holder(h)}")
        else:
            self.on_event("info", f"control: {describe_holder(ident)} has control")
        return {"ok": True, "granted": True, "taken_from": h,
                "control": self._status_locked(now)}

    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        """For every status frame: the holder and everyone heard from lately."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            return self._status_locked(now)

    def _status_locked(self, now: float) -> dict:
        h = self._holder
        # age_s = since last heard; driving = a machine that changed something
        # within DRIVING_S (the bars show "also driving: scan-core")
        clients = [dict(ident, age_s=round(now - seen, 1),
                        driving=now - self._changed.get(ident["id"], -1e9) <= DRIVING_S)
                   for ident, seen in self._clients.values()]
        clients.sort(key=lambda c: c["age_s"])
        # always = verbs a viewer may still send: a panel built from describe
        # (the suite's Control tab) keeps exactly those buttons usable
        return {"holder": dict(h) if h else None, "clients": clients,
                "lease_s": self.lease_s,
                "always": sorted(self.safety | ALWAYS_VERBS)}


# ---------------------------------------------------------------------- #
# the client side
# ---------------------------------------------------------------------- #
class ControlRefused(RuntimeError):
    """The service refused a command because another client holds control."""


class ControlClient:
    """Mix into a module's client class (next to its own ``_rpc``).

    The client then names itself in every request, says "still here" every
    ``HEARTBEAT_S`` (so a viewer is counted and a holder keeps control), and
    has ``take_control`` / ``release_control`` / ``has_control``. The class
    using it must: call ``_control_setup(kind, name)`` in ``__init__``, put
    ``self._with_identity(req)`` into every request it sends, raise with
    ``self._raise_refusal(reply)`` on a refused reply, feed every status frame
    to ``self._control_from_status(payload)``, and start/stop the heartbeat
    with its SUB thread.
    """

    def _control_setup(self, kind: str = "script", name: str = "") -> None:
        self.identity = make_identity(kind, name)
        self._control: dict | None = None
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

    def _with_identity(self, req: dict) -> dict:
        req.setdefault("client", self.identity)
        return req

    @staticmethod
    def _raise_refusal(reply: dict) -> None:
        if reply.get("refused") == "control":
            raise ControlRefused(reply.get("error", "read-only: another client has control"))

    def _control_from_status(self, payload: dict) -> None:
        c = payload.get("control") if isinstance(payload, dict) else None
        if isinstance(c, dict):
            self._control = c

    # -- heartbeat --------------------------------------------------------- #
    def start_heartbeat(self) -> None:
        self._hb_stop.clear()
        if self._hb_thread is None or not self._hb_thread.is_alive():
            self._hb_thread = threading.Thread(target=self._hb_loop, daemon=True,
                                               name="control-heartbeat")
            self._hb_thread.start()

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=1.0)

    def _hb_loop(self) -> None:
        while not self._hb_stop.wait(HEARTBEAT_S):
            try:
                r = self._rpc(cmd="heartbeat")
                self._control_from_status(r)
            except Exception:
                pass      # an old service without control, or a pause: try again

    # -- the verbs --------------------------------------------------------- #
    def take_control(self, force: bool = False) -> bool:
        """Take control; ``force`` takes it over from another holder.

        Returns True when this client holds control afterwards. Without
        ``force`` it only succeeds when control is free (or already ours).
        """
        r = self._rpc(cmd="take_control", force=bool(force))
        self._control_from_status(r)
        return bool(r.get("granted"))

    def release_control(self) -> bool:
        r = self._rpc(cmd="release_control")
        self._control_from_status(r)
        return bool(r.get("released"))

    def control(self) -> dict | None:
        """The service's last word on control: {"holder", "clients", "lease_s"},
        or None when the service does not know about control (an older one)."""
        return self._control

    def has_control(self) -> bool:
        """True when this client, or another client on THIS PC, holds control."""
        h = (self._control or {}).get("holder")
        return bool(h and (h.get("id") == self.identity["id"] or same_pc(h, self.identity)))
