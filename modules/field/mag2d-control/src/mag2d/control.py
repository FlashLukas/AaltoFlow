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
* ONE SCAN AT A TIME per instrument (Lukas, 2026-09-29: "make sure there is
  no more than one scanning core running the same instruments"). A scan
  engine identifies with ``role: "scan"`` and claims every instrument it uses
  (``claim_scan{label}``) before its first point, releasing it at the end.
  While one scan holds the claim, a change from ANY other scan engine -- a
  second suite on the same PC or on another -- is refused, and so is its
  claim. Separate from control on purpose: the camera's autofocus (a machine,
  no scan role) and people keep their rules; only two scans are kept apart.
  The claim lapses like control (no heartbeat for ``lease_s``), so a crashed
  scan frees its instruments.
* A scan needs CONTROL as well: its claim is refused while another PC holds
  control; when nobody does, the scan's PC takes control for the scan and
  gives it back at the end (a GUI elsewhere is a viewer meanwhile).
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
                        "clients", "claim_scan", "release_scan"})
READ_PREFIXES = ("get_", "read_", "list_")
#: Verbs allowed for everyone, always. ``shutdown``: see the module docstring.
ALWAYS_VERBS = frozenset({"shutdown"})

KINDS = ("gui", "script", "machine")


# ------------------------------------------------- the instrument's own PC ---
#
# Security review 2026-11-26. Two kinds of request are accepted only from a
# program running on the instrument's OWN PC, never from another PC on the
# network, whoever holds control:
#
# * a FILE PATH (save_positions{path}, load_pattern{path}, save_picture
#   {folder}...). A path names a file on the service's PC; from another PC it
#   would let that PC overwrite or read any file the service can reach (its
#   own .ini, a startup script...). On the own PC it gives nothing away: that
#   user can write those files anyway.
# * a CHANGE to the SAFETY part of the config: the limits (the envelope every
#   setter clamps to -- a magnet's maximum current, a laser's maximum power),
#   the hardware address and driver files, the interlock, and any setting that
#   names a file. Change them at the instrument. Sending the SAME values is
#   fine, so a remote Settings dialog that sends the whole config still works
#   as long as it leaves these alone.
#
# "Own PC" is decided by the KEY that sent the message (secure.Guard.check
# stamps req["_origin"]), so it needs encryption on. A service that is not
# encrypted has no way to know who sent anything and behaves as before.

#: set by secure.Guard.check on every request: "own" or "other"
ORIGIN_KEY = "_origin"
#: request arguments that name a file or folder on the service's PC
PATH_ARGS = ("path", "folder", "file", "filename", "dir", "directory")
#: config groups that only the own PC may change (also "limits_1", "limits_2"...)
PROTECTED_GROUPS = ("limits", "hardware", "interlock", "safety")
#: config keys (in any group) that name a file or folder
PATH_SUFFIXES = ("_path", "_file", "_dir", "_folder")


def from_other_pc(req) -> bool:
    """True when the key that sent `req` is not the instrument's own PC's."""
    return isinstance(req, dict) and req.get(ORIGIN_KEY) == "other"


def _own_pc_only(what: str) -> dict:
    return {"ok": False, "refused": "own_pc",
            "error": f"refused: {what} is only accepted from the instrument's own PC "
                     f"(security: a file path or a safety setting from another PC "
                     f"could overwrite files or widen the limits). Do it on the PC "
                     f"the service runs on."}


def path_refusal(req) -> dict | None:
    """The refusal for a request from another PC that names a file; else None.
    An EMPTY path/folder is fine: it means "the service's own default"."""
    if not from_other_pc(req):
        return None
    for k in PATH_ARGS:
        v = req.get(k)
        if v not in (None, "") and not (isinstance(v, str) and not v.strip()):
            return _own_pc_only(f"{req.get('cmd')!r} with a file path ({k})")
    return None


def _protected(group: str, key: str) -> bool:
    g = str(group).lower()
    return (g in PROTECTED_GROUPS or g.startswith(tuple(p + "_" for p in PROTECTED_GROUPS))
            or str(key).lower().endswith(PATH_SUFFIXES))


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or str(a).strip().lower() == str(b).strip().lower()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b or abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1.0)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b or str(a) == str(b)


def config_refusal(req, current: dict) -> dict | None:
    """For ``set_config``: the refusal when a request from ANOTHER PC changes a
    protected setting (see above); None when it may go on.

    ``current`` = the service's config as ``get_config`` returns it
    (``config_to_dict(cfg)``). Only keys that exist in it count -- the module
    ignores the others anyway."""
    if not from_other_pc(req):
        return None
    incoming = req.get("config") if isinstance(req, dict) else None
    if not isinstance(incoming, dict) or not isinstance(current, dict):
        return None
    changed = []
    for group, values in incoming.items():
        have = current.get(group)
        if not isinstance(values, dict) or not isinstance(have, dict):
            continue
        for k, v in values.items():
            if k in have and _protected(group, k) and not _same(v, have[k]):
                changed.append(f"{group}.{k}")
    if not changed:
        return None
    return _own_pc_only("changing " + ", ".join(sorted(changed)))


def make_identity(kind: str = "script", name: str = "", role: str = "") -> dict:
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
    ident = {"id": uuid.uuid4().hex, "kind": kind, "name": name or kind,
             "host": f"{user}@{socket.gethostname()}"}
    if role:
        ident["role"] = role      # "scan": the scan engine (see claim_scan)
    return ident


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
    ``clock`` is injectable for tests. ``path_ok`` = verbs whose path
    argument the service CONFINES itself (the scan server's ``get_file`` only
    serves files inside its data folder), so a request from another PC may
    name one; every other verb with a path is refused from another PC
    (path_refusal).
    """

    def __init__(self, safety=(), read=(), lease_s: float = LEASE_S,
                 on_event=None, clock=time.monotonic, path_ok=()):
        self.safety = frozenset(safety)
        self.path_ok = frozenset(path_ok)
        self.read = frozenset(read)
        self.lease_s = float(lease_s)
        self.on_event = on_event or (lambda level, msg: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._holder: dict | None = None       # identity + "since" (wall clock)
        self._holder_seen = 0.0                # monotonic
        self._clients: dict[str, tuple[dict, float]] = {}   # id -> (identity, seen)
        self._changed: dict[str, float] = {}   # machine id -> when it last changed something
        self._scan: dict | None = None          # the scan engine holding the scan claim
        self._scan_seen = 0.0

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
                "name": str(c.get("name") or ""), "host": str(c.get("host") or ""),
                "role": str(c.get("role") or "")}

    def _expire(self, now: float) -> None:
        # caller holds the lock
        if self._holder is not None and now - self._holder_seen > self.lease_s:
            old = self._holder
            self._holder = None
            self.on_event("warn", f"control: {describe_holder(old)} went silent for "
                                  f"{self.lease_s:.0f} s -- control is free")
        if self._scan is not None and now - self._scan_seen > self.lease_s:
            old = self._scan
            self._end_scan()
            self.on_event("warn", f"scan: '{old.get('label')}' ({describe_holder(old)}) "
                                  f"went silent for {self.lease_s:.0f} s -- the "
                                  "instrument is free for another scan")
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
        if self._scan is not None and self._scan["id"] == ident["id"]:
            self._scan_seen = now

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
        refused = None if cmd in self.path_ok else path_refusal(req)
        if refused is not None:
            return refused
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

            if cmd == "claim_scan":
                return self._claim_scan(ident, str(req.get("label") or "scan"), now)
            if cmd == "release_scan":
                if ident is not None and self._scan is not None \
                        and self._scan["id"] == ident["id"]:
                    self._end_scan()
                    return {"ok": True, "released": True}
                return {"ok": True, "released": False}

            if not self.is_write(cmd):
                return None
            # one scan at a time: a SCAN engine that does not hold the scan
            # claim may not change anything while another scan holds it
            if ident is not None and ident.get("role") == "scan" \
                    and self._scan is not None and self._scan["id"] != ident["id"]:
                return {"ok": False, "refused": "scan",
                        "error": self._busy_text(cmd),
                        "control": self._status_locked(now)}
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

    def _end_scan(self) -> None:
        # caller holds the lock. Control the scan took only for itself (nobody
        # had it) goes back to "nobody"; control a person holds stays theirs.
        s = self._scan
        self._scan = None
        h = self._holder
        if s is not None and h is not None and h.get("by_scan") and h["id"] == s["id"]:
            self._holder = None

    def _busy_text(self, cmd=None) -> str:
        # caller holds the lock
        s = self._scan or {}
        since = time.strftime("%H:%M", time.localtime(s.get("since", 0)))
        what = f"{cmd!r} was not sent -- " if cmd else ""
        return (f"busy: scan '{s.get('label')}' from {describe_holder(s)} has been "
                f"using this instrument since {since}; {what}only one scan may drive "
                "an instrument at a time (wait for it to end, or abort it there)")

    def _claim_scan(self, ident: dict | None, label: str, now: float) -> dict:
        # caller holds the lock
        if ident is None:
            return {"ok": False, "error": "claim_scan needs a client identity"}
        if self._scan is not None and self._scan["id"] != ident["id"]:
            return {"ok": False, "refused": "scan", "error": self._busy_text(),
                    "control": self._status_locked(now)}
        # A scan needs CONTROL too (Lukas: "I don't know why you would not want
        # this"): refused while ANOTHER PC holds it; allowed when its own PC
        # does; and when nobody does, the scan's PC takes control for the
        # length of the scan (freed at release_scan / when the scan goes silent)
        # -- a GUI opened meanwhile elsewhere is a viewer, not a second driver.
        h = self._holder
        if h is not None and not same_pc(h, ident):
            since = time.strftime("%H:%M", time.localtime(h.get("since", 0)))
            return {"ok": False, "refused": "control",
                    "error": f"read-only: {describe_holder(h)} has control of this "
                             f"instrument (since {since}); a scan needs control -- take "
                             "control first (a GUI or the suite's Control tab), or ask "
                             "them to release it",
                    "control": self._status_locked(now)}
        if h is None:
            self._holder = dict(ident, since=time.time(), by_scan=True)
            self._holder_seen = now
        fresh = self._scan is None
        self._scan = dict(ident, label=label, since=time.time()) if fresh \
            else dict(self._scan, label=label)
        self._scan_seen = now
        if fresh:
            self.on_event("info", f"scan: '{label}' ({describe_holder(ident)}) started "
                                  "using this instrument")
        return {"ok": True, "granted": True, "control": self._status_locked(now)}

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
                # the scan engine using this instrument right now (label, host,
                # since), or None -- one scan at a time
                "scan": dict(self._scan) if self._scan else None,
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
