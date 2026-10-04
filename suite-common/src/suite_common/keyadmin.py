"""Managing this PC's key and the lab keyring -- the ONE implementation.

Two front ends use it: the "Security..." window in Mission Control (for
people who would rather not type commands) and ``tools/keys.py`` (the same
things from the command line). Both only display what this module returns
and turn its ``AdminError`` into a message, so the window and the command
line can never disagree about what a step does.

What the words mean (README, "Encryption and keys"; secure.py has the
details):

* this PC's KEY: a key pair. The public half is a padlock, one small text
  file that may be shared; the secret half never leaves the PC.
* the KEYRING: a folder (usually on a network share) with one ``<pc>.key``
  file per trusted PC and the lab's ``policy.json``. Whoever can write into
  it is trusted, so only the lab's administrator should be able to.
* MACHINE: a PC whose programs may act as "machine" (scan-core running a
  scan, the camera driving kim) and so pass the control lock.
* the POLICY: ``{"mode": off | warn | enforce, "modules": [...] or ["*"]}``.

Standard library only (plus secure.py); zmq is imported lazily, only when a
key is MADE, so ``keys.py status`` and friends run with any plain Python.
Nothing here prints: the callers decide how to say things.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

from . import secure


class AdminError(RuntimeError):
    """A step could not be done. The message says why, in plain words, and
    what to do about it -- it is shown to the user as it is."""


class NeedsOverwrite(AdminError):
    """The step would replace something that exists (a PC's key in the
    keyring, this PC's own key). Repeat it with overwrite / force = True
    after asking the user."""


#: a PC name in the keyring: it becomes a file name, so keep it simple
_PC_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


# ------------------------------------------------------------------ helpers ---

def _check_pc_name(pc: str) -> str:
    pc = str(pc or "").strip().lower()
    if not _PC_NAME.match(pc):
        raise AdminError(f"'{pc}' cannot be a PC name: use letters, digits, '-', '_' "
                         f"or '.', starting with a letter or digit")
    return pc


def _reachable(folder: Path | None) -> bool:
    """Is the folder there? On a share that is offline, is_dir() may itself
    fail instead of saying False."""
    if folder is None:
        return False
    try:
        return folder.is_dir()
    except OSError:
        return False


def _keyring() -> Path:
    """The keyring this PC uses, or AdminError saying why there is none."""
    kr = secure.keyring_dir()
    if kr is None:
        raise AdminError("this PC uses no keyring yet: choose the keyring folder first "
                         "(python tools/keys.py use <folder>)")
    if not _reachable(kr):
        raise AdminError(f"the keyring folder {kr} is not reachable from this PC "
                         f"(is the share connected?)")
    return kr


def _write_json(path: Path, data: dict, what: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        raise AdminError(f"could not write {what} ({path}): {exc.strerror or exc}. "
                         f"Is the folder reachable and may you write to it?") from None


def _write_settings(keyring: Path) -> None:
    _write_json(secure.security_dir() / secure.SETTINGS_FILE, {"keyring": str(keyring)},
                "this PC's security settings")


def modules_arg(text: str) -> list[str]:
    """'kim, camera' -> ['kim', 'camera']; '*' -> ['*']."""
    return [m.strip().lower() for m in str(text or "").split(",") if m.strip()]


def own_public() -> str | None:
    """This PC's public key, or None when it has no key."""
    try:
        return secure.own_keys()[0]
    except secure.SecurityError:
        return None


# ------------------------------------------------------------------- status ---

@dataclass
class Status:
    """Everything the "This PC" view and `keys.py status` show."""
    pc: str                              # this PC's host name
    security_dir: Path
    has_key: bool = False
    key_pc: str = ""                     # the name this PC's key carries
    public_prefix: str = ""              # first 8 characters of the public key
    keyring: Path | None = None
    keyring_reachable: bool = False
    has_policy_file: bool = False
    in_keyring: bool | None = None       # None: cannot tell (no key / no keyring)
    keyring_name: str = ""               # as which PC this key is in the keyring
    keyring_file: str = ""
    machine: bool | None = None          # None: not in the keyring
    policy: dict = field(default_factory=lambda: {"mode": "off", "modules": []})
    problems: list = field(default_factory=list)   # unreadable key files, with why

    def advice(self) -> list[str]:
        """What is wrong and what to do next, most important first, in plain
        words (the window shows them under the status; ASCII only)."""
        out = []
        if self.keyring is None:
            out.append("This PC uses no keyring yet -> Choose keyring folder... "
                       "(ask the lab's administrator where it is).")
        elif not self.keyring_reachable:
            out.append(f"The keyring folder {self.keyring} is not reachable from this PC "
                       f"-> connect the share, then Refresh.")
        elif not self.has_policy_file:
            out.append("The keyring folder has no policy.json yet, so security is off "
                       "-> set it in the Lab policy tab.")
        if not self.has_key:
            out.append("This PC has no key yet -> Make this PC's key.")
        elif self.in_keyring is False:
            extra = " (in warn mode they will, with a warning)" if \
                self.policy.get("mode") == "warn" else ""
            out.append("This PC's key is not in the keyring, so the other PCs will not let "
                       f"it in{extra} -> Save public key to file... and add it on a PC that "
                       "may write the keyring (Trusted PCs > Add a PC from its key file...).")
        if self.problems:
            out.append(f"{len(self.problems)} key file(s) in the keyring cannot be read from "
                       "this PC -> see the Trusted PCs tab.")
        if not out:
            out.append("All set: this PC has a key and is in the keyring.")
        return out


def status() -> Status:
    d = secure.security_dir()
    st = Status(pc=secure.this_pc_name(), security_dir=d)
    try:
        public, _, pc = secure.own_keys()
        st.has_key, st.key_pc, st.public_prefix = True, pc, public[:8]
    except secure.SecurityError:
        public = None
    st.keyring = secure.keyring_dir()
    st.keyring_reachable = _reachable(st.keyring)
    st.policy = secure.policy()
    if st.keyring_reachable:
        try:
            st.has_policy_file = (st.keyring / secure.POLICY_FILE).is_file()
        except OSError:
            st.has_policy_file = False
        ring = secure.Keyring(st.keyring)
        ring.entries()
        st.problems = list(ring.problems)
        if public:
            e = ring.by_key(public)
            st.in_keyring = e is not None
            if e is not None:
                st.keyring_name, st.keyring_file, st.machine = e.pc, e.file, e.machine
    return st


# ----------------------------------------------------------------- keyring ---

def use_keyring(folder) -> list[str]:
    """Point this PC at a keyring folder. Returns warnings (empty when fine)."""
    kr = Path(folder)
    warnings = []
    if not _reachable(kr):
        warnings.append(f"{kr} is not reachable from this PC right now")
    else:
        try:
            has_policy = (kr / secure.POLICY_FILE).is_file()
        except OSError:
            has_policy = False
        if not has_policy:
            warnings.append(f"{kr} has no {secure.POLICY_FILE} (yet) -- security stays off "
                            f"until it has one")
    _write_settings(kr)
    return warnings


def init_keyring(folder, mode: str = "warn", modules=None, force: bool = False) -> Path:
    """Create a lab keyring (the folder and its policy.json) and point this
    PC at it. Refuses to replace an existing policy unless force."""
    if mode not in secure.MODES:
        raise AdminError(f"unknown mode '{mode}' (one of {', '.join(secure.MODES)})")
    kr = Path(folder)
    try:
        kr.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AdminError(f"could not create {kr}: {exc.strerror or exc}") from None
    if (kr / secure.POLICY_FILE).exists() and not force:
        raise NeedsOverwrite(f"{kr} already has a {secure.POLICY_FILE} (use --force to "
                             f"replace it)")
    _write_json(kr / secure.POLICY_FILE, {"mode": mode, "modules": list(modules or [])},
                "the lab policy")
    _write_settings(kr)
    return kr


# ------------------------------------------------------------- this PC's key ---

@dataclass
class MadeKey:
    pc: str
    public: str
    own_file: Path                     # this PC's public key file (security folder)
    keyring_file: Path | None = None   # where it went in the keyring, or
    local_file: Path | None = None     # ... the copy to bring there by hand
    replaced: bool = False             # an older key of this PC was replaced


def _own_addresses() -> list[str]:
    """This PC's own IPv4 addresses, so that a client that connects by
    address (not by name) still finds the key -- best effort."""
    try:
        return [a for a in socket.gethostbyname_ex(socket.gethostname())[2]
                if not a.startswith("127.")]
    except OSError:
        return []


def make_key(machine: bool = False, pc: str | None = None, force: bool = False,
             addresses=None, here=None) -> MadeKey:
    """Make this PC's key pair and put its public half into the keyring.

    When the keyring is not writable from here, the public key file goes into
    `here` (default: the current folder) instead, to be copied in by hand.
    Refuses (NeedsOverwrite) when this PC already has a key, unless force:
    then the OLD key stops working everywhere, because its secret half is
    gone -- every PC that knew it has to learn the new one from the keyring.
    """
    d = secure.security_dir()
    replaced = (d / secure.OWN_SECRET).exists()
    if replaced and not force:
        raise NeedsOverwrite(f"this PC already has a key ({d / secure.OWN_PUBLIC}); --force "
                             f"makes a new one, and the old one stops working everywhere")
    pc = _check_pc_name(pc or secure.this_pc_name())
    try:
        public, secret = secure.new_keypair()
    except ImportError:
        raise AdminError("making a key needs pyzmq: "
                         "uv run --with pyzmq python tools/keys.py new") from None
    addresses = list(addresses or []) or _own_addresses()
    # "host": the name this PC's programs put into their identity (control.py),
    # so the PC may be called something else in the keyring
    meta = {"pc": pc, "host": socket.gethostname().strip().lower(),
            "machine": "yes" if machine else "no"}
    if addresses:
        meta["addresses"] = " ".join(addresses)
    try:
        secure.write_cert(d / secure.OWN_PUBLIC, public, meta=meta)
        secure.write_cert(d / secure.OWN_SECRET, public, secret, meta=meta)
    except OSError as exc:
        raise AdminError(f"could not write this PC's key into {d}: "
                         f"{exc.strerror or exc}") from None
    made = MadeKey(pc=pc, public=public, own_file=d / secure.OWN_PUBLIC, replaced=replaced)
    kr = secure.keyring_dir()
    try:
        if not _reachable(kr):
            raise OSError("no keyring")
        secure.write_cert(kr / f"{pc}.key", public, meta=meta)
        made.keyring_file = kr / f"{pc}.key"
    except OSError:
        target = Path(here) if here is not None else Path.cwd()
        try:
            secure.write_cert(target / f"{pc}.key", public, meta=meta)
            made.local_file = target / f"{pc}.key"
        except OSError:
            made.local_file = None             # still in the security folder
    return made


def export_public(path) -> Path:
    """Write this PC's PUBLIC key to `path` (safe to share: a USB stick, an
    e-mail, the share) -- to be added to the keyring from another PC."""
    try:
        public, _, meta = secure.read_cert(secure.security_dir() / secure.OWN_PUBLIC)
    except (OSError, secure.SecurityError):
        raise AdminError("this PC has no key yet -- make it first") from None
    path = Path(path)
    try:
        secure.write_cert(path, public, meta=meta)       # never the secret half
    except OSError as exc:
        raise AdminError(f"could not write {path}: {exc.strerror or exc}") from None
    return path


# ------------------------------------------------------------ trusted PCs ---

def entries() -> tuple[list, list[str]]:
    """(the trusted PCs as secure.Entry, the key files that cannot be read
    from this PC with the reason). AdminError when there is no keyring."""
    ring = secure.Keyring(_keyring())
    found = ring.entries()
    return found, list(ring.problems)


def _find_entry(kr: Path, pc: str) -> Path:
    pc = pc.lower()
    for e in secure.Keyring(kr).entries():
        if e.pc == pc or pc in e.names:
            return kr / e.file
    raise AdminError(f"no PC called '{pc}' in the keyring {kr}")


def set_machine(pc: str, yes: bool) -> bool:
    """May programs on `pc` act as a machine (run scans, pass the control
    lock)? Rewrites that PC's key file. Returns the new value."""
    kr = _keyring()
    path = _find_entry(kr, pc)
    public, _, meta = secure.read_cert(path)
    meta["machine"] = "yes" if yes else "no"
    try:
        secure.write_cert(path, public, meta=meta)
    except OSError as exc:
        raise AdminError(f"could not change {path.name}: {exc.strerror or exc} -- "
                         f"may you write to the keyring?") from None
    return bool(yes)


#: retired key files go here, inside the keyring: kept (so a retirement can
#: be undone) but never trusted -- the Keyring reads only *.key at the top
RETIRED_DIR = "retired"


class RetiresThisPC(AdminError):
    """Retiring the PC this runs on: it would lock itself out of every
    secured module on the other PCs. Repeat with force=True after asking."""


@dataclass
class Retired:
    pc: str
    file: Path                 # where the key file is now (keyring/retired/...)
    date: str = ""             # "2026-10-04", from the file name


def _this_pc_names() -> set:
    names = {secure.this_pc_name()}
    try:
        names.add(secure.own_keys()[2])
    except secure.SecurityError:
        pass
    return names


def retire(pc: str, force: bool = False) -> Retired:
    """Stop trusting `pc`: MOVE its key file to <keyring>/retired/<pc>-<date>.key.

    Takes effect within a few seconds (services re-read the keyring every
    RELOAD_S), also for connections that PC has open now: a secured service
    checks the key of every message, not only when the connection is made
    (secure.Guard.check). Moved, not deleted, so restore() can undo it.
    Also works for a key file this PC cannot read (by its file name).
    Refuses (RetiresThisPC) to retire THIS PC unless force."""
    import time as _time
    kr = _keyring()
    name = pc.strip().lower()
    try:
        path = _find_entry(kr, name)
        public, _, meta = secure.read_cert(path)
        who = str(meta.get("pc") or path.stem).lower()
        is_me = public == own_public()
    except AdminError:
        stem = name[:-4] if name.endswith(".key") else name
        path = kr / f"{stem}.key"
        if not path.exists():
            raise
        who, is_me = stem, False
    if not force and (is_me or who in _this_pc_names()):
        raise RetiresThisPC(f"'{who}' is THIS PC: retiring it locks this PC out of every "
                            f"secured module on the other PCs (its own services still "
                            f"answer it)")
    date = _time.strftime("%Y-%m-%d")
    target = kr / RETIRED_DIR / f"{who}-{date}.key"
    if target.exists():                    # retired twice on one day
        target = target.with_name(f"{who}-{date}-{_time.strftime('%H%M%S')}.key")
    try:
        target.parent.mkdir(exist_ok=True)
        path.replace(target)
    except OSError as exc:
        raise AdminError(f"could not move {path.name} out of the keyring: "
                         f"{exc.strerror or exc} -- may you write to the keyring?") from None
    return Retired(pc=who, file=target, date=date)


#: a retired file's name: <pc>-YYYY-MM-DD[-HHMMSS].key
_RETIRED_NAME = re.compile(r"^(?P<pc>.+)-(?P<date>\d{4}-\d{2}-\d{2})(-\d{6})?$")


def retired() -> list[Retired]:
    """The retired PCs (newest first), from <keyring>/retired/."""
    kr = _keyring()
    d = kr / RETIRED_DIR
    out = []
    try:
        files = sorted(d.glob("*.key"), reverse=True) if d.is_dir() else []
    except OSError:
        files = []
    for p in files:
        m = _RETIRED_NAME.match(p.stem)
        out.append(Retired(pc=(m.group("pc") if m else p.stem).lower(), file=p,
                           date=m.group("date") if m else ""))
    return out


def restore(pc: str) -> Path:
    """Undo retire(): move the newest retired key of `pc` (or the retired
    file of that name) back into the keyring as <pc>.key."""
    kr = _keyring()
    name = pc.strip()
    hits = [r for r in retired() if r.pc == name.lower() or r.file.name == name]
    if not hits:
        raise AdminError(f"no retired key of '{pc}' in {kr / RETIRED_DIR}")
    r = hits[0]
    target = kr / f"{r.pc}.key"
    if target.exists():
        raise AdminError(f"'{r.pc}' is in the keyring already ({target.name}): retire or "
                         f"remove that one first")
    try:
        r.file.replace(target)
    except OSError as exc:
        raise AdminError(f"could not move {r.file.name} back: {exc.strerror or exc}") from None
    return target


def remove(pc: str, force: bool = False) -> Retired:
    """The old name of retire() (keys.py remove): the file is kept in
    retired/, not deleted."""
    return retire(pc, force=force)


@dataclass
class KeyFile:
    """What a key file someone brought says about itself."""
    path: Path
    public: str
    pc: str                     # suggested name (its metadata, else the file name)
    machine: bool
    has_secret: bool


def peek_key_file(path) -> KeyFile:
    """Read a key file without changing anything (the window prefills the
    name and the machine box from it)."""
    path = Path(path)
    try:
        public, secret, meta = secure.read_cert(path)
    except PermissionError:
        raise AdminError(f"{path.name} cannot be read from this PC (permissions?)") from None
    except OSError as exc:
        raise AdminError(f"{path.name} cannot be read: {exc.strerror or exc}") from None
    except secure.SecurityError:
        raise AdminError(f"{path.name} is not an AaltoFlow key file") from None
    stem = path.stem.lower()
    pc = str(meta.get("pc") or ("" if stem.startswith("this_pc") else stem)).strip().lower()
    machine = str(meta.get("machine", "no")).strip().lower() in ("yes", "true", "1")
    return KeyFile(path=path, public=public, pc=pc, machine=machine, has_secret=bool(secret))


@dataclass
class Added:
    pc: str
    file: Path
    replaced: bool = False


def add_from_file(path, machine: bool | None = None, pc_name: str | None = None,
                  overwrite: bool = False) -> Added:
    """Trust the PC whose PUBLIC key file is `path`: write it, fresh, into
    the keyring as <pc>.key.

    Why write it fresh instead of copying the file: on the lab's share a
    file keeps the permissions of the PC that wrote it (the share maps Linux
    permissions), so a key file dropped there from the office PC was
    unreadable from the lab PC (2026-09-30). Written from the PC that runs
    this, it is readable wherever that PC's files are.

    Refuses a file that holds a SECRET key (it must never leave its PC, let
    alone sit in a shared folder), a public key that is already in the
    keyring under another name, and -- unless overwrite -- replacing a PC
    that is already there (NeedsOverwrite).
    """
    kf = peek_key_file(path)
    if kf.has_secret:
        raise AdminError(f"{kf.path.name} holds a SECRET key. It must never leave its PC "
                         f"and is never added to the keyring: on that PC use 'Save public "
                         f"key to file...' (or its this_pc.key) instead")
    pc = _check_pc_name(pc_name or kf.pc)
    kr = _keyring()
    found, _ = entries()
    for e in found:
        if e.public == kf.public and e.pc != pc:
            raise AdminError(f"this key is already in the keyring as '{e.pc}' ({e.file}); "
                             f"remove that one first to rename it")
    target = kr / f"{pc}.key"
    same_pc = [kr / e.file for e in found if e.pc == pc and e.file != target.name]
    exists = target.exists() or bool(same_pc)
    if exists and not overwrite:
        raise NeedsOverwrite(f"'{pc}' is already in the keyring: replace its key?")
    _, _, meta = secure.read_cert(kf.path)
    meta = {k: v for k, v in meta.items()}
    meta["pc"] = pc
    if machine is not None:
        meta["machine"] = "yes" if machine else "no"
    meta.setdefault("machine", "no")
    try:
        for old in same_pc:
            old.unlink()
        if target.exists():
            # a file this PC cannot read may still be deletable; writing a
            # NEW file gives it this PC's permissions (the point of adding)
            try:
                target.unlink()
            except OSError:
                pass
        secure.write_cert(target, kf.public, meta=meta)
    except OSError as exc:
        raise AdminError(f"could not write {target.name} into the keyring: "
                         f"{exc.strerror or exc} -- may you write to it?") from None
    return Added(pc=pc, file=target, replaced=exists)


# ------------------------------------------------------------------ policy ---

def get_policy() -> dict:
    """The lab's policy as this PC sees it ({"mode", "modules"}; "off" when
    there is no keyring or no policy file)."""
    return secure.policy()


def set_policy(mode: str | None = None, modules=None) -> dict:
    """Change the lab's policy (None keeps that part). It is LAB-WIDE: every
    PC reads the same file. Returns the new policy."""
    kr = _keyring()
    pol = secure.policy()
    mode = mode or pol["mode"]
    if mode not in secure.MODES:
        raise AdminError(f"unknown mode '{mode}' (one of {', '.join(secure.MODES)})")
    mods = [str(m).strip().lower() for m in modules if str(m).strip()] \
        if modules is not None else pol["modules"]
    _write_json(kr / secure.POLICY_FILE, {"mode": mode, "modules": mods}, "the lab policy")
    return {"mode": mode, "modules": mods}


def wanted_mode(module: str, pol: dict) -> str:
    """The mode a service of `module` should run in under `pol`: the policy's
    mode where it secures the module, "off" (plain) everywhere else."""
    return pol["mode"] if secure.module_secured(module, pol) else "off"


def stale_services(new_policy: dict | None = None) -> list[dict]:
    """The services running on THIS PC whose mode differs from what the
    policy (default: the current one) wants -- they must restart to follow.

    Why: a service picks plain or CurveZMQ once, when it starts (gotcha #47).
    Each running service leaves a marker {"module", "mode", "pid"} in the
    security folder (secure.running_secured); other PCs' services are not
    visible from here and need a restart there."""
    pol = new_policy or secure.policy()
    return [r for r in secure.running_secured()
            if r.get("mode") != wanted_mode(r.get("module", ""), pol)]


def describe_policy(pol: dict) -> str:
    """'off' / 'warn (all modules)' / 'enforce (3 modules)' -- short."""
    mode = pol.get("mode", "off")
    if mode == "off":
        return "off"
    mods = pol.get("modules") or []
    if "*" in mods:
        which = "all modules"
    elif not mods:
        which = "no modules"
    else:
        which = f"{len(mods)} module" + ("" if len(mods) == 1 else "s")
    return f"{mode} ({which})"
