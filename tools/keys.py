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
    python tools/keys.py retire old-laptop      # locks that PC out (kept in retired/)
    python tools/keys.py restore old-laptop     # ... and lets it back in
    python tools/keys.py export my-pc.key       # this PC's PUBLIC key, to bring along
    python tools/keys.py add my-pc.key          # trust the PC of a key file brought here

Services read the policy when they start: restart a module after changing
its mode or module list. Adding or removing a PC works at once (the keyring
is re-read every few seconds).

`new` makes keys with pyzmq (hence `uv run --with pyzmq`); every other
command runs with any plain Python.

The same steps without typing: Mission Control > Security... Both are thin
front ends over suite_common/keyadmin.py, which does the actual work.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "suite-common" / "src"))
from suite_common import keyadmin, secure  # noqa: E402
from suite_common.keyadmin import AdminError  # noqa: E402


def _say_problems(problems, prefix: str = "") -> None:
    """Key files this PC could not use -- a PC otherwise just goes missing.
    On a share that maps Linux permissions, a key file written from ANOTHER PC
    may be unreadable here: add it again from this PC (keys.py add), or make
    it readable for all (a public key may be; only WRITING the keyring must
    be restricted)."""
    if problems:
        print(f"{prefix}! {len(problems)} key file(s) could not be read here:")
        for p in problems:
            print(f"{prefix}    {p}")


# ---------------------------------------------------------------- commands ---

def cmd_status(args) -> int:
    st = keyadmin.status()
    print(f"this PC          : {st.pc}")
    print(f"security folder  : {st.security_dir}")
    if st.has_key:
        print(f"this PC's key    : {st.public_prefix}... (as '{st.key_pc}')")
    else:
        print("this PC's key    : none yet (uv run --with pyzmq python tools/keys.py new)")
    print(f"keyring          : {st.keyring or 'none (python tools/keys.py use <folder>)'}")
    if st.keyring is not None and not st.keyring_reachable:
        print("                   ! not reachable from this PC")
    mods = ", ".join(st.policy["modules"]) or "none"
    print(f"policy           : mode '{st.policy['mode']}', secured modules: {mods}")
    _say_problems(st.problems, prefix="                   ")
    if st.in_keyring is False:
        print("in the keyring   : NO -- other PCs will not let this one in"
              + (" (warn mode: they will, with a warning)" if st.policy["mode"] == "warn"
                 else ""))
    elif st.in_keyring:
        print(f"in the keyring   : yes, as '{st.keyring_name}' ({st.keyring_file}), "
              f"machine = {'yes' if st.machine else 'no'}")
    return 0


def cmd_init(args) -> int:
    kr = keyadmin.init_keyring(args.folder, args.mode, keyadmin.modules_arg(args.modules),
                               force=args.force)
    print(f"keyring created: {kr}")
    print(f"policy: mode '{args.mode}', secured modules: {args.modules or 'none'}")
    print("this PC now uses it. Next, on EVERY lab PC (this one too):")
    print(f"    python tools/keys.py use {kr}")
    print("    uv run --with pyzmq python tools/keys.py new")
    print("Make the folder writable ONLY for the lab's administrator: whoever can")
    print("put a file into it is trusted.")
    return 0


def cmd_use(args) -> int:
    for w in keyadmin.use_keyring(args.folder):
        print(f"warning: {w} (python tools/keys.py init)")
    print(f"this PC uses the keyring {Path(args.folder)}")
    return 0


def cmd_new(args) -> int:
    made = keyadmin.make_key(machine=args.machine, pc=args.pc, force=args.force,
                             addresses=args.address)
    print(f"this PC's key made, as '{made.pc}': {made.own_file}")
    print(f"(the secret half {made.own_file.with_name(secure.OWN_SECRET)} never leaves this PC)")
    if made.keyring_file is not None:
        print(f"public key added to the keyring: {made.keyring_file}")
    else:
        kr = secure.keyring_dir()
        src = made.local_file or made.own_file
        print(f"the keyring is not writable from here: copy {src}")
        print(f"into the keyring folder{f' ({kr})' if kr else ''} -- that is what trusts this PC")
    return 0


def cmd_export(args) -> int:
    path = keyadmin.export_public(args.file)
    print(f"this PC's public key written to {path} (safe to share; on the lab PC:")
    print(f"python tools/keys.py add {path.name})")
    return 0


def cmd_add(args) -> int:
    machine = None if args.machine is None else args.machine == "yes"
    added = keyadmin.add_from_file(args.file, machine=machine, pc_name=args.pc,
                                   overwrite=args.force)
    print(f"{'replaced' if added.replaced else 'added'} '{added.pc}': {added.file}")
    return 0


def cmd_list(args) -> int:
    entries, problems = keyadmin.entries()
    _say_problems(problems)
    kr = secure.keyring_dir()
    if not entries:
        print(f"no PCs in {kr} yet")
    else:
        own = keyadmin.own_public()
        print(f"{'PC':20s} {'machine':8s} {'key':12s} addresses")
        for e in entries:
            mark = "  <- this PC" if e.public == own else ""
            print(f"{e.pc:20s} {'yes' if e.machine else 'no':8s} {e.public[:8] + '...':12s} "
                  f"{' '.join(e.addresses)}{mark}")
    gone = keyadmin.retired()
    if gone:
        print("retired (not trusted; python tools/keys.py restore <pc> undoes it):")
        for r in gone:
            print(f"  {r.pc:20s} {r.date}")
    return 0


def cmd_machine(args) -> int:
    keyadmin.set_machine(args.pc, args.value == "yes")
    print(f"{args.pc}: machine = {args.value}")
    return 0


def cmd_policy(args) -> int:
    mods = keyadmin.modules_arg(args.modules) if args.modules is not None else None
    new = keyadmin.set_policy(args.mode, mods)
    print(f"policy: mode '{new['mode']}', secured modules: "
          f"{', '.join(new['modules']) or 'none'}")
    print("restart the secured modules' services for a change of mode or list")
    # A running service keeps the mode it started with. Clients follow it
    # (secure.no_answer: one timeout, then the other mode), but name the
    # ones on this PC so they get restarted. The policy is lab-wide: other
    # PCs' services are not listed here.
    stale = keyadmin.stale_services(new)
    if stale:
        print("still running in their old mode on this PC (restart them):")
        for r in stale:
            print(f"  {r.get('module')}  (pid {r.get('pid')}, mode '{r.get('mode')}')")
    return 0


def cmd_retire(args) -> int:
    r = keyadmin.retire(args.pc, force=args.force)
    print(f"retired {r.pc}: it no longer reaches any secured module (open connections "
          f"included, within a few seconds)")
    print(f"its key file is kept in {r.file} -- python tools/keys.py restore {r.pc} undoes it")
    return 0


def cmd_restore(args) -> int:
    path = keyadmin.restore(args.pc)
    print(f"restored {path.name}: {args.pc} is trusted again")
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

    for name in ("retire", "remove"):          # "remove" is the old name
        p = sub.add_parser(name, help="stop trusting a PC (its key file is kept in "
                                      "retired/, so it can be restored)")
        p.add_argument("pc")
        p.add_argument("--force", action="store_true",
                       help="also when it is THIS PC (it locks itself out)")
        p.set_defaults(fn=cmd_retire)

    p = sub.add_parser("restore", help="trust a retired PC again")
    p.add_argument("pc")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("export", help="write this PC's PUBLIC key to a file, to bring along")
    p.add_argument("file")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("add", help="trust the PC whose public key file this is "
                                   "(writes it fresh into the keyring)")
    p.add_argument("file")
    p.add_argument("--machine", choices=("yes", "no"),
                   help="may its programs act as 'machine' (default: what the file says)")
    p.add_argument("--pc", help="its name in the keyring (default: what the file says)")
    p.add_argument("--force", action="store_true", help="replace that PC's key if it is there")
    p.set_defaults(fn=cmd_add)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except AdminError as exc:
        # keyadmin says what went wrong and what to do; no traceback
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    raise SystemExit(main())
