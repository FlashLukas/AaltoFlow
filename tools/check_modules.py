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
  * ports do not clash with another module's
  * start_after names modules that exist, without a cycle
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
  * it answers `shutdown` and EXITS BY ITSELF within 15 s (the launcher asks
    for this before it kills a service; a killed service cannot close its
    hardware -- docs/DEVELOPER_NOTES.md gotcha #25)
Exit code 0 when nothing failed.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "suite-common" / "src"))
from suite_common.modules import (MANIFEST, discover_local, port_conflicts,  # noqa: E402
                                  start_order)

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


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, module, check, result, detail=""):
        self.rows.append((module, check, result, detail))

    @property
    def failed(self):
        return [r for r in self.rows if r[2] == "FAIL"]

    def print(self):
        width = max([len(r[0]) for r in self.rows] + [6])
        for module, check, result, detail in self.rows:
            print(f"  {result:4s}  {module:<{width}}  {check}" + (f"  -- {detail}" if detail else ""))


def venv_python(d: Path) -> Path | None:
    for cand in (d / ".venv" / "Scripts" / "python.exe", d / ".venv" / "bin" / "python"):
        if cand.exists():
            return cand
    return None


def help_text(py: Path, d: Path, script: str) -> str:
    r = subprocess.run([str(py), script, "--help"], cwd=d, capture_output=True,
                       text=True, timeout=120)
    return r.stdout + r.stderr


def free_port_pair() -> tuple[int, int]:
    """Two free ports next to each other, picked by the OS."""
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cmd = s.getsockname()[1]
        if cmd >= 65535:
            continue
        with socket.socket() as a, socket.socket() as b:
            try:
                a.bind(("127.0.0.1", cmd)); b.bind(("127.0.0.1", cmd + 1))
                return cmd, cmd + 1
            except OSError:
                continue


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

    rep = Report()
    mods, problems = discover_local(args.root)
    for p in problems:
        rep.add(Path(p.split(":")[0]).parent.name or "?", f"{MANIFEST} parses", "FAIL", p)
    keys = {m.key for m in mods}
    for c in port_conflicts(mods):
        rep.add("suite", "ports unique", "FAIL", c)

    if args.modules:
        unknown = set(args.modules) - keys
        for k in sorted(unknown):
            rep.add(k, "module exists", "FAIL", "no module.toml with this key")
        mods = [m for m in mods if m.key in args.modules]

    ordered = start_order(mods)
    for m in mods:
        rep.add(m.key, f"{MANIFEST} parses", "PASS", f"{m.dir.name}, ports {m.cmd}/{m.pub}")
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

    rep.print()
    n_fail = len(rep.failed)
    print(f"\n{len(rep.rows)} checks, {n_fail} failed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
