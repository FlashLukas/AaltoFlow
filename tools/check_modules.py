"""Check every module against the suite's module contract.

    python tools/check_modules.py            # static checks, a few seconds
    python tools/check_modules.py --live     # also start each service and ask it to describe itself
    python tools/check_modules.py hf2 --live # just one

The contract (INSTRUMENT_MODULE_GUIDE.md section 11) is what lets the launcher
and scan-core handle a module they have never heard of. A module that breaks it
does not fail loudly -- it just is not listed, or its Start button passes a flag
the script does not know, or scan-core connects to the wrong port. This finds
those before the lab does.

Static checks, per module:
  * module.toml parses; key, name, ports, scripts are valid
  * it sits where it belongs, modules/<category>/<folder> with the category of
    its module.toml (WARN, not FAIL: discovery still finds a module in the old
    flat place or under another category folder -- it is just harder to find
    for a person). A key found twice is a WARN naming both folders.
  * ports do not clash with another module's
  * start_after names modules that exist, without a cycle
  * src/<pkg>/control.py and src/<pkg>/apps/control_bar.py (one controller,
    many viewers) and src/<pkg>/secure.py (CurveZMQ encryption -- being
    rolled out module by module), WHERE PRESENT, are byte-identical to the
    masters in suite-common/src/suite_common/
  * src/<pkg>/hwlock.py exists and is byte-identical to the master copy
    suite-common/src/suite_common/hwlock.py (FAIL otherwise: a stale copy may
    normalise addresses differently, and then two modules would not see that
    they hold the same instrument)
  * every real backend (src/<pkg>/backends/*.py except base.py, sim*.py and the
    remote_*.py ZeroMQ clients) calls claim( somewhere -- a plain text search,
    so it is a WARN, not a FAIL: the one physical address, one service rule
    (docs/DEVELOPER_NOTES.md, "one address, one service")
  * icon.svg exists and is well-formed XML
  * run_service.py accepts --cmd-port, --pub-port and --real
  * run_gui.py (if any) accepts --connect, --cmd-port and --pub-port
  (the last two run `--help` with the module's own .venv python; a module that
   has never been synced is reported as SKIP, not FAIL)

Live checks (--live), per module with a .venv:
  * the service starts on a SCRATCH port pair (never its real ports, so a
    running lab service is not disturbed)
  * it answers `describe` within 30 s, the manifest's "module" equals the key,
    and it has parameters
  * if any parameter declares a `stream` (for fly scans), the service answers
    stream_start / stream_read / stream_stop, and the reply carries every
    declared channel with as many values as time stamps
  * a MALFORMED request (bytes that are not JSON, then a JSON array instead of
    an object) is ANSWERED with {"ok": false, ...}, and `describe` still
    answers afterwards on a fresh socket. A REP socket that received a request
    and sent nothing back refuses everything after it: one bad message would
    take the whole command port down (docs/DEVELOPER_NOTES.md gotcha #39)
  * it answers `shutdown` and EXITS BY ITSELF within 15 s (the launcher asks
    for this before it kills a service; a killed service cannot close its
    hardware -- docs/DEVELOPER_NOTES.md gotcha #25)
  * PORT TAKEN: with the scratch command port already occupied by another
    socket, the service must EXIT with a non-zero code within 20 s. One that
    keeps running is deaf (nothing can reach it) but may hold the instrument
    and its hwlock claim -- the sockets must be bound before the instrument is
    opened (gotcha #39)
Exit code 0 when nothing failed (warnings do not count).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "suite-common" / "src"))
from suite_common.modules import (MANIFEST, MODULES_DIR, discover_local,  # noqa: E402
                                  is_legacy_location, port_conflicts, rel_to_root,
                                  start_order)

# The address lock. Every module carries its OWN copy (modules are installed
# without suite-common, like theme.py), so the copies must stay identical to
# this master or two modules could disagree on what "the same address" is.
HWLOCK_MASTER = ROOT / "suite-common" / "src" / "suite_common" / "hwlock.py"
# Control (one controller, many viewers): the same copy-per-module rule.
CONTROL_MASTER = ROOT / "suite-common" / "src" / "suite_common" / "control.py"
CONTROL_BAR_MASTER = ROOT / "suite-common" / "src" / "suite_common" / "control_bar.py"
SECURE_MASTER = ROOT / "suite-common" / "src" / "suite_common" / "secure.py"

# Asks a service to describe itself, run by the MODULE's own python (which has
# pyzmq) so this checker needs nothing beyond the standard library.
_DESCRIBE = r"""
import json, sys, zmq
s = zmq.Context.instance().socket(zmq.REQ)
s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, 2000)
s.connect(f"tcp://127.0.0.1:{sys.argv[1]}")
try:
    s.send_json({"cmd": "describe"}); r = s.recv_json()
    print(json.dumps(r.get("describe") if r.get("ok") else None))
except zmq.Again:
    print("null")
"""

# The fly-scan stream verbs, for a module whose manifest declares a stream:
# start, let it record, read, stop. Prints the two stream replies, or null.
_STREAM = r"""
import json, sys, time, zmq
s = zmq.Context.instance().socket(zmq.REQ)
s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, 3000)
s.connect(f"tcp://127.0.0.1:{sys.argv[1]}")
out = []
try:
    for verb in ("stream_start", "stream_read", "stream_stop"):
        s.send_json({"cmd": verb}); r = s.recv_json()
        if not r.get("ok"):
            out = None; break
        if verb != "stream_start":
            out.append(r.get("stream"))
        time.sleep(0.3)
    print(json.dumps(out))
except zmq.Again:
    print("null")
"""

# Same, for the `shutdown` verb: prints the reply, or null on no answer.
_SHUTDOWN = r"""
import json, sys, zmq
s = zmq.Context.instance().socket(zmq.REQ)
s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, 3000)
s.connect(f"tcp://127.0.0.1:{sys.argv[1]}")
try:
    s.send_json({"cmd": "shutdown"}); print(json.dumps(s.recv_json()))
except zmq.Again:
    print("null")
"""

# A malformed request, twice: raw bytes that are not JSON, then valid JSON that
# is not an object. Each must be ANSWERED (a REP socket that received and did
# not reply is stuck for good), with ok false. Then a FRESH socket asks
# `describe`, to prove the command port still works. Prints one JSON line.
_MALFORMED = r"""
import json, sys, zmq
ctx = zmq.Context.instance()
def req():
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, 3000)
    s.connect(f"tcp://127.0.0.1:{sys.argv[1]}")
    return s
out = {"replies": [], "describe_after": False}
for payload in (b"this is not JSON {", b"[1, 2, 3]"):
    s = req()
    try:
        s.send(payload); raw = s.recv()
        try:
            r = json.loads(raw.decode("utf-8"))
        except Exception:
            r = {"_unparsable": raw[:60].decode("latin-1")}
    except zmq.Again:
        r = None
    out["replies"].append(r)
    s.close(0)
s = req()
try:
    s.send_json({"cmd": "describe"}); out["describe_after"] = bool(s.recv_json().get("ok"))
except zmq.Again:
    pass
print(json.dumps(out))
"""


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, module, check, result, detail=""):
        self.rows.append((module, check, result, detail))

    @property
    def failed(self):
        return [r for r in self.rows if r[2] == "FAIL"]

    @property
    def warned(self):
        return [r for r in self.rows if r[2] == "WARN"]

    def print(self):
        width = max([len(r[0]) for r in self.rows] + [6])
        for module, check, result, detail in self.rows:
            print(f"  {result:4s}  {module:<{width}}  {check}" + (f"  -- {detail}" if detail else ""))


def venv_python(d: Path) -> Path | None:
    # A tree inside OneDrive keeps its venvs OUT of the project (dev.ps1 puts
    # them in %LOCALAPPDATA%\uv-venvs\<folder>, gotcha #8), so look there too.
    ext = Path(os.environ.get("LOCALAPPDATA", "")) / "uv-venvs" / d.name
    for cand in (d / ".venv" / "Scripts" / "python.exe", d / ".venv" / "bin" / "python",
                 ext / "Scripts" / "python.exe"):
        if cand.exists():
            return cand
    return None


def package_dir(d: Path) -> Path | None:
    """src/<pkg>: the one folder under src/ that is a Python package."""
    src = d / "src"
    if not src.is_dir():
        return None
    pkgs = sorted(p for p in src.iterdir() if (p / "__init__.py").is_file())
    return pkgs[0] if len(pkgs) == 1 else None


def control_check(rep: Report, m):
    """control.py / apps/control_bar.py, where a module has them, are the
    master copies (suite-common/src/suite_common/). A stale copy could let a
    viewer change what the others refuse, or speak an older control protocol.
    A module without them is not yet part of the rollout: reported as INFO in
    the detail, not failed."""
    pkg = package_dir(m.dir)
    if pkg is None:
        return
    for rel, master in (("control.py", CONTROL_MASTER),
                        ("apps/control_bar.py", CONTROL_BAR_MASTER),
                        ("secure.py", SECURE_MASTER)):
        copy = pkg / rel
        name = f"{rel} is the master copy"
        if not copy.is_file():
            continue
        if not master.is_file():
            rep.add(m.key, name, "FAIL", f"master {master} missing")
        elif copy.read_bytes() != master.read_bytes():
            rep.add(m.key, name, "FAIL",
                    f"src/{pkg.name}/{rel} differs: copy suite-common/src/suite_common/"
                    f"{Path(rel).name}")
        else:
            rep.add(m.key, name, "PASS")


def hwlock_check(rep: Report, m, master: bytes | None):
    """The module's hwlock.py is the master copy, and its real backends claim."""
    pkg = package_dir(m.dir)
    if pkg is None:
        rep.add(m.key, "hwlock.py is the master copy", "FAIL", "no single package under src/")
        return
    copy = pkg / "hwlock.py"
    fix = "copy suite-common/src/suite_common/hwlock.py"
    if not copy.is_file():
        rep.add(m.key, "hwlock.py is the master copy", "FAIL", f"src/{pkg.name}/hwlock.py missing: {fix}")
    elif master is not None and copy.read_bytes() != master:
        rep.add(m.key, "hwlock.py is the master copy", "FAIL", f"src/{pkg.name}/hwlock.py differs: {fix}")
    else:
        rep.add(m.key, "hwlock.py is the master copy", "PASS")

    # Static text check: a real backend that opens an instrument must claim
    # its address first. The simulator never claims; base.py is the Protocol;
    # remote_*.py talk to ANOTHER service over ZeroMQ and own no hardware.
    backends = pkg / "backends"
    if not backends.is_dir():
        return
    silent = []
    for f in sorted(backends.glob("*.py")):
        if f.name in ("__init__.py", "base.py") or f.name.startswith(("sim", "remote_")):
            continue
        if "claim(" not in f.read_text(encoding="utf-8", errors="replace"):
            silent.append(f.name)
    if silent:
        rep.add(m.key, "real backends claim their address", "WARN",
                "no claim( in backends/" + ", backends/".join(silent))
    else:
        rep.add(m.key, "real backends claim their address", "PASS")


def help_text(py: Path, d: Path, script: str) -> str:
    r = subprocess.run([str(py), script, "--help"], cwd=d, capture_output=True,
                       text=True, timeout=120)
    return r.stdout + r.stderr


def free_port_pair() -> tuple[int, int]:
    """Two free ports next to each other, OUTSIDE the OS's ephemeral range.

    Port 0 would let the OS pick -- but from the ephemeral range (49152-65535
    on Windows), which is exactly where every outgoing connection (the checker's
    own REQ probes, the test runs next door) takes its local port. Such a port
    could be grabbed between our check and the service's bind; a service that
    binds before it opens anything (gotcha #39) then rightly refuses to start,
    and the check would blame the module. 20000-40000 is outside that range."""
    while True:
        cmd = random.randrange(20000, 40000)
        with socket.socket() as a, socket.socket() as b:
            try:
                a.bind(("127.0.0.1", cmd)); b.bind(("127.0.0.1", cmd + 1))
                return cmd, cmd + 1
            except OSError:
                continue


def stream_check(rep: Report, m, py: Path, cmd: int, manifest: dict):
    """A declared `stream` must work: scan-core's fly scan relies on it."""
    channels = {p["stream"].get("channel") for p in manifest.get("parameters", [])
                if isinstance(p.get("stream"), dict)}
    if not channels:
        return
    r = subprocess.run([str(py), "-c", _STREAM, str(cmd)], capture_output=True,
                       text=True, timeout=30)
    try:
        chunks = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        chunks = None
    problems = []
    if not chunks:
        problems.append("stream_start / stream_read / stream_stop not answered ok")
    else:
        for c in chunks:
            c = c or {}
            t = c.get("t") or []
            vals = c.get("values") or {}
            missing = channels - set(vals)
            if missing:
                problems.append(f"channels missing from the reply: {sorted(missing)}")
            if any(len(v) != len(t) for v in vals.values()):
                problems.append("a channel has a different length than its time stamps")
            if "now" not in c:
                problems.append("no `now` in the reply (clock offset cannot be estimated)")
        if not problems and not (chunks[0] or {}).get("t"):
            problems.append("recorded nothing in 0.3 s")
    rep.add(m.key, "live: stream verbs work", "FAIL" if problems else "PASS",
            "; ".join(sorted(set(problems))) if problems
            else f"{len(channels)} channel(s), {len(chunks[0]['t'])} samples in 0.3 s")


def malformed_check(rep: Report, m, py: Path, cmd: int) -> bool:
    """A malformed request is answered, and the port keeps working.

    Returns False when the command port is dead afterwards, so the caller can
    skip the `shutdown` check instead of blaming it for this failure."""
    r = subprocess.run([str(py), "-c", _MALFORMED, str(cmd)], capture_output=True,
                       text=True, timeout=30)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {"replies": [], "describe_after": False}
    problems = []
    for what, reply in zip(("non-JSON bytes", "a JSON array"), res.get("replies", [])):
        if reply is None:
            problems.append(f"{what}: no reply within 3 s")
        elif not isinstance(reply, dict) or reply.get("ok") is not False:
            problems.append(f"{what}: reply is not ok:false ({str(reply)[:60]})")
    if not res.get("describe_after"):
        problems.append("describe no longer answered afterwards")
    rep.add(m.key, "live: malformed request answered, port survives",
            "FAIL" if problems else "PASS", "; ".join(problems))
    return bool(res.get("describe_after"))


def port_taken_check(rep: Report, m, py: Path):
    """With its command port already taken, the service must EXIT non-zero.

    The blocker is a plain listening TCP socket. On Windows it needs
    SO_EXCLUSIVEADDRUSE: without it, Windows lets the service's bind on
    0.0.0.0 share a port that is taken on 127.0.0.1, and nothing would clash.
    Loopback only, so no firewall prompt."""
    cmd, pub = free_port_pair()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind(("127.0.0.1", cmd))
    blocker.listen(1)
    proc = subprocess.Popen([str(py), m.service, "--cmd-port", str(cmd), "--pub-port", str(pub)],
                            cwd=m.dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        try:
            out, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            rep.add(m.key, "live: exits non-zero when its port is taken", "FAIL",
                    f"still running 20 s with port {cmd} taken: a deaf service")
            return
        # the reason is on stderr (one line, if the module does it right)
        last = (err or "").strip().splitlines()[-1:] or (out or "").strip().splitlines()[-1:] or [""]
        rep.add(m.key, "live: exits non-zero when its port is taken",
                "PASS" if proc.returncode != 0 else "FAIL",
                f"exit {proc.returncode}: {last[0][:100]}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        blocker.close()


def live_check(rep: Report, m, py: Path):
    cmd, pub = free_port_pair()
    proc = subprocess.Popen([str(py), m.service, "--cmd-port", str(cmd), "--pub-port", str(pub)],
                            cwd=m.dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        manifest, deadline = None, time.monotonic() + 30
        while time.monotonic() < deadline and proc.poll() is None:
            r = subprocess.run([str(py), "-c", _DESCRIBE, str(cmd)], capture_output=True,
                               text=True, timeout=30)
            try:
                manifest = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                manifest = None
            if manifest:
                break
            time.sleep(0.5)
        if proc.poll() is not None:
            out = (proc.stdout.read() or "").strip().splitlines()[-3:]
            rep.add(m.key, "live: service starts on scratch ports", "FAIL",
                    f"exited with {proc.returncode}: {' | '.join(out)}")
            return
        if not manifest:
            rep.add(m.key, "live: answers describe", "FAIL", f"no answer on {cmd} within 30 s")
            return
        rep.add(m.key, "live: answers describe", "PASS", f"on scratch port {cmd}")
        got = manifest.get("module")
        rep.add(m.key, "live: describe 'module' equals the key",
                "PASS" if got == m.key else "FAIL", "" if got == m.key else f"says {got!r}")
        n = len(manifest.get("parameters", []))
        rep.add(m.key, "live: describe has parameters", "PASS" if n else "FAIL", f"{n}")
        stream_check(rep, m, py, cmd, manifest)
        if not malformed_check(rep, m, py, cmd):
            rep.add(m.key, "live: stops cleanly on `shutdown`", "SKIP",
                    "command port dead after the malformed request")
            return

        r = subprocess.run([str(py), "-c", _SHUTDOWN, str(cmd)], capture_output=True,
                           text=True, timeout=30)
        try:
            reply = json.loads(r.stdout.strip().splitlines()[-1]) or {}
        except (ValueError, IndexError):
            reply = {}
        t0 = time.monotonic()
        try:
            proc.wait(15)
            exited = True
        except subprocess.TimeoutExpired:
            exited = False
        ok = bool(reply.get("ok")) and exited
        detail = (f"exited in {time.monotonic() - t0:.1f} s" if ok else
                  f"reply {reply or 'none'}, " + ("exited" if exited else "still running after 15 s"))
        rep.add(m.key, "live: stops cleanly on `shutdown`", "PASS" if ok else "FAIL", detail)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("modules", nargs="*", help="keys to check (default: all)")
    ap.add_argument("--live", action="store_true", help="start each service and query it")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.live:
        # The live checks test the wire contract, not this PC's keys: run the
        # services and the probes with security off (an empty security folder,
        # secure.py), also on a lab PC whose policy secures some modules.
        import tempfile
        os.environ["AALTOFLOW_SECURITY_DIR"] = tempfile.mkdtemp(prefix="aaltoflow-check-")

    rep = Report()
    mods, problems = discover_local(args.root)
    for p in problems:
        if " exists twice: " in p:
            # discovery used one copy and says which; worth fixing, not fatal
            rep.add(p.split("'")[1] if "'" in p else "?", "module found once", "WARN", p)
            continue
        # A problem reads "<folder>\module.toml: why". Cut at the file NAME,
        # not at the first ':' -- that is the drive letter's colon in C:\...
        cut = p.find(MANIFEST)
        name = Path(p[:cut]).name if cut > 0 else ""
        rep.add(name or "?", f"{MANIFEST} parses", "FAIL", p)
    keys = {m.key for m in mods}
    for c in port_conflicts(mods):
        rep.add("suite", "ports unique", "FAIL", c)
    master = HWLOCK_MASTER.read_bytes() if HWLOCK_MASTER.is_file() else None
    if master is None:
        rep.add("suite", "hwlock master copy present", "FAIL", str(HWLOCK_MASTER))

    if args.modules:
        unknown = set(args.modules) - keys
        for k in sorted(unknown):
            rep.add(k, "module exists", "FAIL", "no module.toml with this key")
        mods = [m for m in mods if m.key in args.modules]

    ordered = start_order(mods)
    for m in mods:
        where = rel_to_root(args.root, m.dir)
        rep.add(m.key, f"{MANIFEST} parses", "PASS", f"{where}, ports {m.cmd}/{m.pub}")
        home = f"{MODULES_DIR}/{m.category}/{m.dir.name}"
        if is_legacy_location(args.root, m.dir):
            rep.add(m.key, "sits in modules/<category>/", "WARN",
                    f"{where} is the old flat place; it belongs in {home} "
                    "(git mv it, or run tools/migrate_layout.py)")
        elif where != home:
            rep.add(m.key, "sits in modules/<category>/", "WARN",
                    f"{where}, but its {MANIFEST} says category {m.category!r}: "
                    f"expected {home}")
        missing = [k for k in m.start_after if k not in keys]
        rep.add(m.key, "start_after names existing modules", "FAIL" if missing else "PASS",
                ", ".join(missing))
        before = {x.key for x in ordered[:ordered.index(m)]}
        cyc = [k for k in m.start_after if k in keys and k not in before
               and m.key in next((x.start_after for x in mods if x.key == k), [])]
        if cyc:
            rep.add(m.key, "no start_after cycle", "FAIL", f"cycle with {', '.join(cyc)}")

        if m.icon is None:
            rep.add(m.key, "icon.svg present", "FAIL", "missing (the launcher shows a letter)")
        else:
            try:
                ET.parse(m.icon)
                rep.add(m.key, "icon.svg well-formed", "PASS")
            except ET.ParseError as exc:
                rep.add(m.key, "icon.svg well-formed", "FAIL", str(exc))
        hwlock_check(rep, m, master)
        control_check(rep, m)

        py = venv_python(m.dir)
        if py is None:
            rep.add(m.key, "command-line contract", "SKIP", "no .venv (run uv sync first)")
            continue
        text = help_text(py, m.dir, m.service)
        miss = [f for f in ("--cmd-port", "--pub-port", "--real") if f not in text]
        rep.add(m.key, "run_service accepts --cmd-port --pub-port --real",
                "FAIL" if miss else "PASS", "missing " + ", ".join(miss) if miss else "")
        if m.gui:
            text = help_text(py, m.dir, m.gui)
            miss = [f for f in ("--connect", "--cmd-port", "--pub-port") if f not in text]
            rep.add(m.key, "run_gui accepts --connect --cmd-port --pub-port",
                    "FAIL" if miss else "PASS", "missing " + ", ".join(miss) if miss else "")
        if args.live:
            live_check(rep, m, py)
            port_taken_check(rep, m, py)

    rep.print()
    n_fail = len(rep.failed)
    n_warn = len(rep.warned)
    print(f"\n{len(rep.rows)} checks, {n_fail} failed"
          + (f", {n_warn} warning(s)" if n_warn else ""))
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
