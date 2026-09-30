"""Encryption and who-is-who on the wire (CurveZMQ).

Why: every module is a network service, and without this anybody who can
reach its port can read the traffic, send it commands, pretend to be it, or
claim to be "machine" (which passes the control lock). CurveZMQ -- built into
ZeroMQ, nothing extra to install -- fixes all four:

* every PC has a KEY PAIR: a public key (a padlock, handed out freely, one
  small file) and a secret key (never leaves the PC);
* the lab has a KEYRING: a folder with the public key file of every trusted
  PC, plus ``policy.json`` (below). Only the person running the lab should be
  able to write to it -- whoever can drop a file there is trusted;
* a SERVICE accepts a connection only from a key in the keyring, and learns
  for every message which PC's key sent it. It then refuses a message whose
  ``client`` identity claims another PC, or claims kind "machine" when the
  keyring does not allow that PC to act as a machine (control.py);
* a CLIENT (GUI, script, scan-core, another module) knows the public key of
  the PC it connects to, from the keyring, so an impostor cannot answer;
* everything, commands and PUB/SUB telemetry, is encrypted.

Keys belong to PCs, not to modules (like control: "control belongs to a
PC"). A new module, GUI or script on a trusted PC needs nothing.

Files
  this PC      ``security_dir()`` (%LOCALAPPDATA%\\AaltoFlow\\security, or
               ~/.local/state/AaltoFlow/security):
                 security.json    {"keyring": "<the keyring folder>"}
                 this_pc.key      this PC's public key (+ metadata)
                 this_pc.key_secret   ... and its secret key: private
  keyring      <pc>.key           one per trusted PC: public key, and
                                  metadata pc / machine / addresses
               policy.json        {"mode": "off" | "warn" | "enforce",
                                   "modules": ["kim", "camera"] or ["*"]}

``policy.json`` sits in the keyring so that the whole lab switches together.
``modules`` lists the modules that speak CurveZMQ: while the rollout is under
way, a module that is not listed keeps talking plain, and the clients (which
read the same list) talk plain to it. ``warn`` encrypts and checks, but lets
unknown keys and false identities through with a warning in the service's
log -- the mode to switch on first. ``enforce`` refuses them.

Tools: ``tools/keys.py`` makes this PC's keys, points the PC at the keyring,
and edits the policy (README, "Encryption and keys").

The environment variable AALTOFLOW_SECURITY_DIR moves ``security_dir()``:
the tests use it so that the security setup of the PC running them never
changes what they test.

Standard library at import; ``zmq`` is imported only where a socket or a key
is made, so the launcher's tools can import this with any plain Python.

MASTER COPY: suite-common/src/suite_common/secure.py. A module that speaks
CurveZMQ carries a byte-identical copy as src/<pkg>/secure.py (the module must
not depend on suite-common); tools/check_modules.py compares them.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("off", "warn", "enforce")
SETTINGS_FILE = "security.json"
POLICY_FILE = "policy.json"
OWN_PUBLIC = "this_pc.key"
OWN_SECRET = "this_pc.key_secret"
#: host names that mean "this PC" to a client
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "*"}
#: the keyring is re-read at most this often (it may be on a network share)
RELOAD_S = 2.0


class SecurityError(RuntimeError):
    """The security setup is incomplete: no key for this PC, no keyring, no
    key for the PC a client wants to reach. The message says what to do."""


# --------------------------------------------------------------- settings ---

def security_dir() -> Path:
    """This PC's private security folder (its keys, and where the keyring is)."""
    env = os.environ.get("AALTOFLOW_SECURITY_DIR")
    if env:
        return Path(env)
    root = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "state")
    return Path(root) / "AaltoFlow" / "security"


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def keyring_dir() -> Path | None:
    """The lab keyring this PC uses, or None when none is set up."""
    k = _read_json(security_dir() / SETTINGS_FILE).get("keyring")
    return Path(k) if k else None


def policy() -> dict:
    """The lab's policy: {"mode", "modules"}. Mode "off" when anything is
    missing -- a PC that was never set up behaves exactly as before."""
    kr = keyring_dir()
    data = _read_json(kr / POLICY_FILE) if kr else {}
    mode = str(data.get("mode", "off")).lower()
    if mode not in MODES:
        mode = "off"
    mods = data.get("modules") or []
    if isinstance(mods, str):
        mods = [mods]
    return {"mode": mode, "modules": [str(m).lower() for m in mods]}


def module_secured(module: str, pol: dict | None = None) -> bool:
    """True when `module` speaks CurveZMQ under the lab's policy.

    `module` is a module key or the name of one instance of it: "kim",
    "kim@pc-a:5567" (a remote service in the launcher) and "kim_pc-a" (the
    same, as the measurement suite spells it) all count as kim."""
    pol = pol or policy()
    if pol["mode"] == "off":
        return False
    name = str(module or "").lower()
    for m in pol["modules"]:
        if m == "*" or name == m or name.startswith((m + "_", m + "@")):
            return True
    return False


# ------------------------------------------------------ certificate files ---
#
# The same text format as zmq.auth.create_certificates (ZPL), so the files
# also load with zmq.auth.load_certificate:
#
#     metadata
#         pc = "lab-pc-1"
#     curve
#         public-key = "rq:rM>}U?@Lns47E1%kR.o@n%FcmmsL/@{H8]yf7"

_LINE = re.compile(r'^\s*([A-Za-z0-9_-]+)\s*=\s*"(.*)"\s*$')


def read_cert(path: Path) -> tuple[str, str | None, dict]:
    """(public key, secret key or None, metadata) of one certificate file."""
    public = secret = None
    meta: dict = {}
    section = ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line[0].isspace():
                section = line.strip()
                continue
            m = _LINE.match(line)
            if not m:
                continue
            k, v = m.group(1), m.group(2)
            if section == "curve" and k == "public-key":
                public = v
            elif section == "curve" and k == "secret-key":
                secret = v
            elif section == "metadata":
                meta[k] = v
    if not public or len(public) != 40:
        raise SecurityError(f"{path}: not a key file (no 40-character public-key)")
    return public, secret, meta


def write_cert(path: Path, public: str, secret: str | None = None,
               meta: dict | None = None) -> None:
    lines = ["#   AaltoFlow key file (CurveZMQ). The public key may be shared;",
             "#   a *.key_secret file must never leave its PC.", "metadata"]
    for k, v in (meta or {}).items():
        lines.append(f'    {k} = "{v}"')
    lines += ["curve", f'    public-key = "{public}"']
    if secret:
        lines.append(f'    secret-key = "{secret}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def new_keypair() -> tuple[str, str]:
    """A fresh (public, secret) key pair, both as 40-character z85 text."""
    import zmq
    pub, sec = zmq.curve_keypair()
    return pub.decode("ascii"), sec.decode("ascii")


def this_pc_name() -> str:
    return socket.gethostname().strip().lower()


def own_keys() -> tuple[str, str, str]:
    """(public, secret, pc name) of this PC; SecurityError if it has none."""
    d = security_dir()
    try:
        public, _, meta = read_cert(d / OWN_PUBLIC)
        _, secret, _ = read_cert(d / OWN_SECRET)
    except (OSError, SecurityError) as exc:
        raise SecurityError(
            f"this PC has no AaltoFlow key yet ({exc}) -- run "
            f"'uv run --with pyzmq python tools/keys.py new' once") from None
    if not secret:
        raise SecurityError(f"{d / OWN_SECRET} holds no secret-key")
    return public, secret, str(meta.get("pc") or this_pc_name()).lower()


# ---------------------------------------------------------------- keyring ---

@dataclass
class Entry:
    """One trusted PC."""
    pc: str
    public: str
    machine: bool = False                  # may send kind "machine"
    addresses: tuple = ()                  # other names / IPs clients use for it
    host: str = ""                         # its real host name, if not `pc`
    file: str = ""
    names: set = field(default_factory=set)

    def __post_init__(self):
        self.names = {self.pc.lower(), *(a.lower() for a in self.addresses)}
        if self.host:
            self.names.add(self.host.lower())


def _entry_from(path: Path, problems: list | None = None) -> Entry | None:
    """The Entry in a key file, or None -- and then WHY in `problems`.

    It used to return None silently: on the lab share a key file written from
    another PC was unreadable (the share maps Linux permissions: a new file is
    readable by its owner and group only), and that PC simply was not in the
    keyring, with no hint why (2026-09-30)."""
    try:
        public, _, meta = read_cert(path)
    except (OSError, SecurityError) as exc:
        if problems is not None:
            why = "cannot be read (permissions?)" if isinstance(exc, PermissionError) \
                else str(exc)
            problems.append(f"{path.name}: {why}")
        return None
    pc = str(meta.get("pc") or path.stem).strip().lower()
    addrs = tuple(a for a in re.split(r"[\s,;]+", str(meta.get("addresses", ""))) if a)
    machine = str(meta.get("machine", "no")).strip().lower() in ("yes", "true", "1")
    return Entry(pc=pc, public=public, machine=machine, addresses=addrs,
                 host=str(meta.get("host", "")).strip().lower(), file=path.name)


class Keyring:
    """The trusted PCs, read from the keyring folder (re-read when it changes,
    at most every RELOAD_S -- deleting a PC's file locks it out)."""

    def __init__(self, folder: Path | None):
        self.folder = Path(folder) if folder else None
        self._lock = threading.Lock()
        self._checked = -1e9
        self._stamp: tuple = ()
        self._by_key: dict[str, Entry] = {}
        #: key files that could not be used, with the reason (keys.py and the
        #: service's log say so, instead of a PC silently missing)
        self.problems: list[str] = []

    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._checked < RELOAD_S:
            return
        self._checked = now
        if self.folder is None or not self.folder.is_dir():
            self._by_key, self._stamp = {}, ()
            return
        files = sorted(self.folder.glob("*.key"))
        stamp = tuple((p.name, p.stat().st_mtime_ns) for p in files)
        if stamp == self._stamp:
            return
        by_key, problems = {}, []
        for p in files:
            e = _entry_from(p, problems)
            if e is not None:
                by_key[e.public] = e
        self._by_key, self._stamp, self.problems = by_key, stamp, problems

    def entries(self) -> list[Entry]:
        with self._lock:
            self._refresh()
            return sorted(self._by_key.values(), key=lambda e: e.pc)

    def by_key(self, public: str) -> Entry | None:
        with self._lock:
            self._refresh()
            return self._by_key.get(public)

    def by_host(self, host: str) -> Entry | None:
        h = str(host).strip().strip("[]").lower()
        short = h.split(".")[0]
        for e in self.entries():
            if h in e.names or short == e.pc:
                return e
        return None


# ----------------------------------------------------------------- client ---

def secure_client(sock, host: str, module: str) -> bool:
    """Make `sock` (REQ or SUB, not yet connected) a CurveZMQ client for
    `module` on `host`, when the lab's policy secures that module.

    Returns False (socket untouched: plain, as before) when it does not.
    Raises SecurityError, saying what to do, when it does but this PC has no
    key or the keyring has no key for `host`.
    """
    pol = policy()
    if not module_secured(module, pol):
        return False
    public, secret, pc = own_keys()
    h = str(host).strip().strip("[]").lower()
    if h in LOCAL_HOSTS or h == pc or h.split(".")[0] == pc or h == this_pc_name():
        server = public                       # a service on this PC uses our key
    else:
        e = Keyring(keyring_dir()).by_host(h)
        if e is None:
            raise SecurityError(
                f"no key for '{host}' in the keyring ({keyring_dir()}): {module} "
                f"there is secured -- copy that PC's key file into the keyring, "
                f"or add '{host}' to its 'addresses'")
        server = e.public
    sock.curve_secretkey = secret.encode("ascii")
    sock.curve_publickey = public.encode("ascii")
    sock.curve_serverkey = server.encode("ascii")
    return True


# ----------------------------------------------------------------- server ---

class Guard:
    """The server side for one service: who may connect (ZAP ``callback``) and
    whether a message's identity matches the key that sent it (``check``)."""

    def __init__(self, module: str, mode: str, keyring: Keyring,
                 own_public: str, own_pc: str, on_event=None):
        self.module = module
        self.mode = mode
        self.keyring = keyring
        self.own_public = own_public
        self.own_pc = own_pc
        self.on_event = on_event or (lambda level, msg: None)
        self.domain = ""
        self._warned: set = set()

    def entry(self, public: str) -> Entry | None:
        """The PC behind a key. This PC's own key is always trusted, and may
        act as a machine unless the keyring says otherwise: a program on the
        service's own PC (the camera driving kim) is the usual machine."""
        e = self.keyring.by_key(public)
        for problem in self.keyring.problems:     # once each, in the service log
            self._say("file:" + problem, "warn", f"keyring file skipped -- {problem}")
        if e is None and public == self.own_public:
            e = Entry(pc=self.own_pc, public=public, machine=True)
        return e

    def _say(self, key: str, level: str, msg: str) -> None:
        if key in self._warned:                # once per problem, not per message
            return
        self._warned.add(key)
        self.on_event(level, f"security: {msg}")

    # ZAP: may this key connect at all?
    def callback(self, domain, key) -> bool:
        public = key.decode("ascii") if isinstance(key, bytes) else str(key)
        if self.entry(public) is not None:
            return True
        if self.mode == "warn":
            self._say("conn:" + public, "warn",
                      f"a PC whose key is not in the keyring connected (key "
                      f"{public[:8]}...); 'warn' lets it in, 'enforce' will not")
            return True
        self._say("conn:" + public, "warn",
                  f"refused a connection: key {public[:8]}... is not in the keyring")
        return False

    def check(self, req: dict, user_id: str | None) -> dict | None:
        """None when `req` may go on; otherwise the refusal to send back."""
        e = self.entry(user_id or "")
        client = req.get("client") if isinstance(req, dict) else None
        if not isinstance(client, dict):
            return None                        # no identity claimed: control decides
        if e is None:                          # only possible in "warn"
            return None
        problems = []
        # the PC part of "user@PC" (control.py) must be a name of the key's PC
        claimed = str(client.get("host") or "").rpartition("@")[2].strip().lower()
        if claimed and claimed not in e.names and claimed.split(".")[0] not in e.names:
            problems.append(f"claims to be on '{claimed}' but its key is {e.pc}'s")
        if client.get("kind") == "machine" and not e.machine:
            problems.append(f"claims to be a machine, and {e.pc} may not act as "
                            f"one (keyring: machine = no)")
        if not problems:
            return None
        text = f"{client.get('name') or 'a client'} " + "; ".join(problems)
        if self.mode == "warn":
            self._say("id:" + text, "warn", text + " ('warn' lets it through)")
            return None
        self._say("id:" + text, "warn", "refused: " + text)
        return {"ok": False, "refused": "security", "error": "refused (security): " + text}


#: the fixed address where libzmq asks "may this key connect?" (the ZAP RFC)
ZAP_ENDPOINT = "inproc://zeromq.zap.01"


class ZapHandler:
    """Answers libzmq's "may this key connect?" questions for one context.

    Why our own and not pyzmq's ThreadAuthenticator (found on the lab PC,
    2026-09-30): that one runs an ASYNCIO loop in its thread, and on Windows
    the default (proactor) loop cannot watch zmq sockets unless `tornado` is
    installed. Without it the thread died at start -- no key was ever answered
    (a secured service is deaf) -- and its stop() then waited forever for the
    dead thread (a service could not shut down; a test run hung for hours).
    This is a plain thread with a plain poll: no asyncio, no extra package, and
    ``stop()`` returns within about a second whatever happened.

    ``providers`` maps a ZAP domain (one per service, see secure_server) to
    its Guard. A CURVE handshake on a known domain is decided by that Guard's
    ``callback``; the reply's User-Id is the client's key (z85), which is what
    ``user_id(frame)`` reads on every received message. Anything else (NULL on
    a plain socket that happens to share the context) is let through, as
    libzmq would without a handler; CURVE on an unknown domain is refused.
    """

    POLL_MS = 200

    def __init__(self, ctx):
        self.ctx = ctx
        self.providers: dict = {}
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="aaltoflow-zap")

    def start(self, timeout: float = 5.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout) or self._error is not None:
            self._stop.set()
            raise SecurityError(
                "the key checker (ZAP) did not start"
                + (f": {self._error}" if self._error else " in time")
                + " -- the service would not answer any encrypted client")

    def alive(self) -> bool:
        return self._thread.is_alive() and self._error is None

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        import zmq
        sock = None
        try:
            sock = self.ctx.socket(zmq.REP)
            sock.linger = 0
            sock.bind(ZAP_ENDPOINT)
        except BaseException as exc:          # e.g. another handler already bound
            # close it: an open socket makes the context's term() wait forever
            if sock is not None:
                sock.close(0)
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                if not sock.poll(self.POLL_MS):
                    continue
                try:
                    msg = sock.recv_multipart()
                except zmq.ZMQError:
                    break
                # a REP socket that received MUST answer (gotcha #39), and this
                # loop must never die: an answer that cannot be built is "500"
                try:
                    reply = self._answer(msg)
                except Exception as exc:
                    reply = [b"1.0", msg[1] if len(msg) > 1 else b"", b"500",
                             str(exc).encode("utf-8", "replace")[:200], b"", b""]
                sock.send_multipart(reply)
        finally:
            sock.close(0)

    def _answer(self, msg: list) -> list:
        from zmq.utils import z85
        # request: version, request id, domain, address, identity, mechanism, credentials...
        if len(msg) < 6:
            return [b"1.0", b"", b"500", b"malformed ZAP request", b"", b""]
        version, request_id, domain, _addr, _identity, mechanism = msg[:6]
        creds = msg[6:]
        if mechanism == b"CURVE" and creds:
            key = z85.encode(creds[0])
            guard = self.providers.get(domain.decode("utf-8", "replace"))
            try:
                ok = guard is not None and bool(guard.callback(domain, key))
            except Exception:
                ok = False                     # a crashing check must not let anyone in
            if ok:
                return [version, request_id, b"200", b"OK", key, b""]
            return [version, request_id, b"400", b"not in the keyring", b"", b""]
        return [version, request_id, b"200", b"OK", b"", b""]


_auth_lock = threading.Lock()
_auths: dict = {}                              # id(context) -> [ZapHandler, n]


def secure_server(ctx, sockets, module: str, on_event=None) -> Guard | None:
    """Make `sockets` (REP, PUB; not yet bound) CurveZMQ servers for `module`,
    when the lab's policy secures it. Returns the Guard (pass every request
    through ``guard.check(req, frame.get("User-Id"))``), or None: plain, as
    before. Undo with ``release_server(guard)`` when the service stops."""
    pol = policy()
    if not module_secured(module, pol):
        return None
    public, secret, pc = own_keys()
    guard = Guard(module, pol["mode"], Keyring(keyring_dir()), public, pc, on_event)
    with _auth_lock:
        slot = _auths.get(id(ctx))
        if slot is not None and not slot[0].alive():
            _auths.pop(id(ctx), None)          # a dead checker: start a fresh one
            slot = None
        if slot is None:
            handler = ZapHandler(ctx)
            handler.start()                    # raises if it cannot run
            slot = _auths[id(ctx)] = [handler, 0]
        slot[1] += 1
        # one ZAP domain per service, so several services in one process
        # (the tests) each get their own guard
        guard.domain = f"aaltoflow-{module}-{id(guard):x}"
        slot[0].providers[guard.domain] = guard
    guard._ctx_id = id(ctx)
    for s in sockets:
        s.zap_domain = guard.domain.encode("ascii")
        s.curve_secretkey = secret.encode("ascii")
        s.curve_publickey = public.encode("ascii")
        s.curve_server = True
    on_event and on_event("info", f"security: {module} is encrypted (CurveZMQ, "
                                  f"mode '{pol['mode']}')")
    return guard


def release_server(guard: Guard | None) -> None:
    """The service stopped: forget its guard (and stop the authenticator
    thread once no service in this process needs it)."""
    if guard is None:
        return
    with _auth_lock:
        slot = _auths.get(getattr(guard, "_ctx_id", None))
        if slot is None:
            return
        slot[0].providers.pop(guard.domain, None)
        slot[1] -= 1
        if slot[1] <= 0:
            slot[0].stop()                     # returns within ~2 s, never hangs
            _auths.pop(guard._ctx_id, None)


def user_id(frame) -> str | None:
    """The key (z85) that sent a received frame (``sock.recv(copy=False)``)."""
    try:
        return frame.get("User-Id")
    except Exception:
        return None
