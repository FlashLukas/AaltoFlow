"""This PC's AaltoFlow key and the lab keyring (CurveZMQ encryption).

What it is for, in the README section "Encryption and keys"; how it works,
in suite-common/src/suite_common/secure.py. In short: every PC has a key
pair, the lab keyring (a folder only the lab's administrator can write to)
holds the public key of every trusted PC and the policy, and a secured
module talks only to PCs in the keyring.

Setting up the lab (once, on any PC):
    python tools/keys.py init \\\\server\\share\\aaltoflow-keyring --mode warn --modules kim,camera

Adding a PC (on that PC):
    python tools/keys.py use \\\\server\\share\\aaltoflow-keyring
    uv run --with pyzmq python tools/keys.py new
        -> writes this PC's key; its public half goes into the keyring
           (or, when the keyring is read-only here, next to you: copy it in)

Everyday:
    python tools/keys.py status                 # this PC: key, keyring, policy
    python tools/keys.py list                   # the trusted PCs
    python tools/keys.py machine lab-pc-1 yes   # lab-pc-1 may act as a machine
    python tools/keys.py policy --mode enforce  # after a week of 'warn'
    python tools/keys.py remove old-laptop      # locks that PC out

Services read the policy when they start: restart a module after changing
its mode or module list. Adding or removing a PC works at once (the keyring
is re-read every few seconds).

`new` makes keys with pyzmq (hence `uv run --with pyzmq`); every other
command runs with any plain Python.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "suite-common" / "src"))
from suite_common import secure  # noqa: E402


def _keyring_or_die() -> Path:
    kr = secure.keyring_dir()
    if kr is None:
        raise SystemExit("this PC uses no keyring yet: python tools/keys.py use <folder>")
    if not kr.is_dir():
        raise SystemExit(f"the keyring folder {kr} is not reachable from this PC")
    return kr


def _write_settings(keyring: Path) -> None:
    d = secure.security_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / secure.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"keyring": str(keyring)}, f, indent=2)


def _write_policy(keyring: Path, mode: str, modules: list[str]) -> None:
    with open(keyring / secure.POLICY_FILE, "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "modules": modules}, f, indent=2)


def _modules_arg(text: str) -> list[str]:
    return [m.strip().lower() for m in text.split(",") if m.strip()]


def _find_entry(keyring: Path, pc: str) -> Path:
    pc = pc.lower()
    for e in secure.Keyring(keyring).entries():
        if e.pc == pc or pc in e.names:
            return keyring / e.file
    raise SystemExit(f"no PC called '{pc}' in the keyring {keyring}")


# ---------------------------------------------------------------- commands ---

def _say_problems(ring, prefix: str = "") -> None:
    """Key files this PC could not use -- a PC otherwise just goes missing.
    On a share that maps Linux permissions, a key file written from ANOTHER PC
    may be unreadable here: write it from a PC every other PC can read, or
    make it readable for all (a public key may be; only WRITING the keyring
    must be restricted)."""
    ring.entries()                       # reads the folder
    if ring.problems:
        print(f"{prefix}! {len(ring.problems)} key file(s) could not be read here:")
        for p in ring.problems:
            print(f"{prefix}    {p}")


def cmd_status(args) -> int:
    d = secure.security_dir()
    print(f"this PC          : {secure.this_pc_name()}")
    print(f"security folder  : {d}")
    try:
        public, _, pc = secure.own_keys()
        print(f"this PC's key    : {public[:8]}... (as '{pc}')")
    except secure.SecurityError:
        public = None
        print("this PC's key    : none yet (uv run --with pyzmq python tools/keys.py new)")
    kr = secure.keyring_dir()
    print(f"keyring          : {kr or 'none (python tools/keys.py use <folder>)'}")
    if kr is not None and not kr.is_dir():
        print("                   ! not reachable from this PC")
    pol = secure.policy()
    mods = ", ".join(pol["modules"]) or "none"
    print(f"policy           : mode '{pol['mode']}', secured modules: {mods}")
    if kr is not None and kr.is_dir():
        _say_problems(secure.Keyring(kr), prefix="                   ")
    if kr is not None and kr.is_dir() and public:
        e = secure.Keyring(kr).by_key(public)
        if e is None:
            print("in the keyring   : NO -- other PCs will not let this one in"
                  + (" (warn mode: they will, with a warning)" if pol["mode"] == "warn" else ""))
        else:
            print(f"in the keyring   : yes, as '{e.pc}' ({e.file}), "
                  f"machine = {'yes' if e.machine else 'no'}")
    return 0


def cmd_init(args) -> int:
    kr = Path(args.folder)
    kr.mkdir(parents=True, exist_ok=True)
    if (kr / secure.POLICY_FILE).exists() and not args.force:
        raise SystemExit(f"{kr} already has a {secure.POLICY_FILE} (use --force to replace it)")
    _write_policy(kr, args.mode, _modules_arg(args.modules))
    _write_settings(kr)
    print(f"keyring created: {kr}")
    print(f"policy: mode '{args.mode}', secured modules: {args.modules or 'none'}")
    print("this PC now uses it. Next, on EVERY lab PC (this one too):")
    print(f"    python tools/keys.py use {kr}")
    print("    uv run --with pyzmq python tools/keys.py new")
    print("Make the folder writable ONLY for the lab's administrator: whoever can")
    print("put a file into it is trusted.")
    return 0


def cmd_use(args) -> int:
    kr = Path(args.folder)
    if not (kr / secure.POLICY_FILE).is_file():
        print(f"warning: {kr} has no {secure.POLICY_FILE} (yet) -- security stays off "
              f"until it has one (python tools/keys.py init)")
    _write_settings(kr)
    print(f"this PC uses the keyring {kr}")
    return 0


def cmd_new(args) -> int:
    d = secure.security_dir()
    if (d / secure.OWN_SECRET).exists() and not args.force:
        raise SystemExit(f"this PC already has a key ({d / secure.OWN_PUBLIC}); --force makes "
                         f"a new one, and the old one stops working everywhere")
    try:
        public, secret = secure.new_keypair()
    except ImportError:
        raise SystemExit("making a key needs pyzmq: uv run --with pyzmq python tools/keys.py new")
    pc = (args.pc or secure.this_pc_name()).lower()
    addresses = list(args.address or [])
    if not args.address:
        # this PC's own IPv4 addresses, so that a client that connects by
        # address (not by name) still finds the key -- best effort
        try:
            addresses = [a for a in socket.gethostbyname_ex(socket.gethostname())[2]
                         if not a.startswith("127.")]
        except OSError:
            addresses = []
    # "host": the name this PC's programs put into their identity (control.py),
    # so the PC may be called something else in the keyring
    meta = {"pc": pc, "host": socket.gethostname().strip().lower(),
            "machine": "yes" if args.machine else "no"}
    if addresses:
        meta["addresses"] = " ".join(addresses)
    secure.write_cert(d / secure.OWN_PUBLIC, public, meta=meta)
    secure.write_cert(d / secure.OWN_SECRET, public, secret, meta=meta)
    print(f"this PC's key made, as '{pc}': {d / secure.OWN_PUBLIC}")
    print(f"(the secret half {d / secure.OWN_SECRET} never leaves this PC)")
    kr = secure.keyring_dir()
    target = f"{pc}.key"
    try:
        if kr is None or not kr.is_dir():
            raise OSError("no keyring")
        shutil.copyfile(d / secure.OWN_PUBLIC, kr / target)
        print(f"public key added to the keyring: {kr / target}")
    except OSError:
        shutil.copyfile(d / secure.OWN_PUBLIC, Path.cwd() / target)
        print(f"the keyring is not writable from here: copy {Path.cwd() / target}")
        print(f"into the keyring folder{f' ({kr})' if kr else ''} -- that is what trusts this PC")
    return 0


def cmd_list(args) -> int:
    kr = _keyring_or_die()
    ring = secure.Keyring(kr)
    entries = ring.entries()
    _say_problems(ring)
    if not entries:
        print(f"no PCs in {kr} yet")
        return 0
    try:
        own = secure.own_keys()[0]
    except secure.SecurityError:
        own = None
    print(f"{'PC':20s} {'machine':8s} {'key':12s} addresses")
    for e in entries:
        mark = "  <- this PC" if e.public == own else ""
        print(f"{e.pc:20s} {'yes' if e.machine else 'no':8s} {e.public[:8] + '...':12s} "
              f"{' '.join(e.addresses)}{mark}")
    return 0


def cmd_machine(args) -> int:
    kr = _keyring_or_die()
    path = _find_entry(kr, args.pc)
    public, _, meta = secure.read_cert(path)
    meta["machine"] = "yes" if args.value == "yes" else "no"
    secure.write_cert(path, public, meta=meta)
    print(f"{args.pc}: machine = {meta['machine']}")
    return 0


def cmd_policy(args) -> int:
    kr = _keyring_or_die()
    pol = secure.policy()
    mode = args.mode or pol["mode"]
    mods = _modules_arg(args.modules) if args.modules is not None else pol["modules"]
    _write_policy(kr, mode, mods)
    print(f"policy: mode '{mode}', secured modules: {', '.join(mods) or 'none'}")
    print("restart the secured modules' services for a change of mode or list")
    return 0


def cmd_remove(args) -> int:
    kr = _keyring_or_die()
    path = _find_entry(kr, args.pc)
    path.unlink()
    print(f"removed {path.name}: {args.pc} is no longer trusted")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="this PC's key, keyring and policy").set_defaults(fn=cmd_status)

    p = sub.add_parser("init", help="create a lab keyring with its policy")
    p.add_argument("folder")
    p.add_argument("--mode", choices=secure.MODES, default="warn")
    p.add_argument("--modules", default="", help="comma list of secured modules, or *")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("use", help="point this PC at the lab keyring")
    p.add_argument("folder")
    p.set_defaults(fn=cmd_use)

    p = sub.add_parser("new", help="make this PC's key and add it to the keyring")
    p.add_argument("--pc", help="name of this PC in the keyring (default: its host name)")
    p.add_argument("--address", action="append",
                   help="another name or IP clients use for this PC (repeatable)")
    p.add_argument("--machine", action="store_true",
                   help="programs on this PC may act as 'machine' on other PCs")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_new)

    sub.add_parser("list", help="the trusted PCs").set_defaults(fn=cmd_list)

    p = sub.add_parser("machine", help="may a PC's programs act as 'machine'?")
    p.add_argument("pc")
    p.add_argument("value", choices=("yes", "no"))
    p.set_defaults(fn=cmd_machine)

    p = sub.add_parser("policy", help="change the mode or the secured modules")
    p.add_argument("--mode", choices=secure.MODES)
    p.add_argument("--modules", help="comma list of secured modules, or *")
    p.set_defaults(fn=cmd_policy)

    p = sub.add_parser("remove", help="stop trusting a PC")
    p.add_argument("pc")
    p.set_defaults(fn=cmd_remove)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
