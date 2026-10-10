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
    for a person). A key found twice is a WARN naming both folders. A SUITE
    PROJECT's module.toml (scan-core's scan server) stays in the root on
    purpose: PASS, and no hwlock / copied control.py / secure.py are asked of
    it (it owns no hardware and imports the suite-common masters; the
    encryption check runs with the master secure.py).
  * ports do not clash with another module's
  * start_after names modules that exist, without a cycle
  * src/<pkg>/control.py and src/<pkg>/apps/control_bar.py (one controller,
    many viewers), src/<pkg>/secure.py (CurveZMQ encryption -- being
    rolled out module by module) and src/<pkg>/follow.py (a setting that
    follows another module through a formula) and src/<pkg>/softramp.py (a knob
    walked at a set pace, for fly scans), WHERE PRESENT, are byte-identical to the
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
  * a declared [hardware] probe (module.toml) exists -- else the manifest does
    not parse -- and prints ONE valid JSON line {"devices": [...], "note": ...}
    with exit code 0, twice: as it is, and with its vendor SDK made MISSING
    (the usual vendor packages blocked, ctypes unable to load a DLL). A probe
    only lists, so running it is safe on a PC with the instrument attached.
  (these run with the module's own .venv python; a module that has never been
   synced is reported as SKIP, not FAIL)

Live checks (--live), per module with a .venv:
  * the service starts on a SCRATCH port pair (never its real ports, so a
    running lab service is not disturbed)
  * it answers `describe` within 30 s, the manifest's "module" equals the key,
    and it has parameters
  * if any parameter declares a `stream` (for fly scans), the service answers
    stream_start / stream_read / stream_stop, and the reply carries every
    declared channel with as many values as time stamps
  * if a control declares a `ramp` (a knob the module sweeps, for fly scans
    over any knob), a short sweep is started on the scratch service: its
    number must show up finished in status, its readback stream must have
    recorded it, and the stop verb must answer
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
# Follow (a setting follows another module's value through a formula): optional,
# copied into the modules that use it (hf2 first).
FOLLOW_MASTER = ROOT / "suite-common" / "src" / "suite_common" / "follow.py"
# The software ramp (a knob walked at a set pace, for fly scans over any knob):
# optional, copied into the modules that sweep a knob themselves (dssg first).
SOFTRAMP_MASTER = ROOT / "suite-common" / "src" / "suite_common" / "softramp.py"

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

# A declared `ramp` (a knob the module sweeps continuously, for fly scans):
# start the readback stream, start a SHORT sweep (~0.5 s at the module's
# default rate, towards the middle of the knob's range), wait for the status
# to show that sweep's number finished, read the stream, and stop. argv:
# port, JSON {id, read_path, scale, min, max, ramp}. Prints one JSON line.
_RAMP = r"""
import json, sys, time, zmq
port, d = sys.argv[1], json.loads(sys.argv[2])
r = d["ramp"]; scale = float(d.get("scale") or 1.0)
s = zmq.Context.instance().socket(zmq.REQ)
s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, 5000)
s.connect(f"tcp://127.0.0.1:{port}")
def ask(**m):
    s.send_json(m); return s.recv_json()
def at(st, path):
    for k in path or []:
        st = st[k] if isinstance(st, list) else st.get(k)
    return st
out = {"ok": False, "why": "", "samples": 0}
try:
    st = ask(cmd="status")["status"]
    here = float(at(st, d["read_path"])) / scale
    lo = d.get("min"); hi = d.get("max")
    rate = float(r["rate"].get("default") or r["rate"].get("max") or 1.0)
    span = rate * 0.5
    mid = (lo + hi) / 2 if lo is not None and hi is not None else here + span
    to = here + span if mid >= here else here - span
    rb = (r.get("readback") or {}).get("stream") or {}
    if rb:
        ask(cmd=rb.get("start_verb", "stream_start"))
    a = r["start"]["args"]
    rep = ask(cmd=r["start"]["verb"], **{a["to"]: to * scale, a["rate"]: rate * scale},
              **(r["start"].get("extra") or {}))
    if not rep.get("ok"):
        out["why"] = f"start refused: {rep.get('error')}"
    else:
        rid = rep.get("ramp_id")
        dn = r.get("done") or {}
        key, idk = dn.get("key", "ramping"), dn.get("id_key", "ramp_id")
        t0 = time.monotonic(); done = False
        while time.monotonic() - t0 < 10.0:
            st = ask(cmd="status")["status"]
            if (rid is None or (st.get(idk) or 0) >= rid) and not st.get(key):
                done = True; break
            time.sleep(0.05)
        n = 0
        if rb:
            c = ask(cmd=rb.get("stop_verb", "stream_stop")).get("stream") or {}
            n = len((c.get("values") or {}).get(rb.get("channel"), []))
        stop = ask(cmd=r["stop"]["verb"], **(r["stop"].get("extra") or {}))
        out["samples"] = n
        if not done:
            out["why"] = "the sweep did not end (status never showed its ramp_id finished)"
        elif rb and n < 2:
            out["why"] = f"the readback stream recorded {n} sample(s)"
        elif not stop.get("ok"):
            out["why"] = f"stop refused: {stop.get('error')}"
        else:
            out["ok"] = True
except zmq.Again:
    out["why"] = "no reply"
except Exception as exc:
    out["why"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""

# A detector read as a BINARY reply part (`read.binary`, guide 6b "Binary
# replies"; the camera's image): run its acquire step if it has one (trigger,
# then poll status until the ready rule's id is taken up and the busy flag is
# down), ask for the value WITH binary (header + parts must agree: dtype x
# shape = bytes, each part named in `binary`) and WITHOUT (one JSON part --
# a plain client must still work), and compare both with the declared dims
# lengths and max. argv: port, JSON descriptor. Prints one JSON line.
_BINARY = r"""
import base64, json, sys, time, zmq
port, d = sys.argv[1], json.loads(sys.argv[2])
s = zmq.Context.instance().socket(zmq.REQ)
s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, 8000)
s.connect(f"tcp://127.0.0.1:{port}")
def ask(**m):
    s.send_json(m); return s.recv_multipart()
out = {"ok": False, "why": ""}
try:
    import numpy as np
    acq = d.get("acquire") or {}
    if acq.get("trigger_verb"):
        head = json.loads(ask(cmd=acq["trigger_verb"])[0])
        if not head.get("ok"):
            raise RuntimeError(f"trigger refused: {head.get('error')}")
        rd = acq.get("ready") or {}
        n = head.get(acq.get("target_key")) if acq.get("target_key") else None
        t0 = time.monotonic()
        while True:
            st = json.loads(ask(cmd="status")[0])["status"]
            adopted = n is None or st.get(rd.get("setpoint_key")) == n
            busy = bool(st.get(rd.get("flag_key")))
            if adopted and (busy if not rd.get("invert") else not busy):
                break
            if time.monotonic() - t0 > float(acq.get("timeout_s", 10)):
                raise RuntimeError("the acquisition did not finish")
            time.sleep(0.05)
    r = d["read"]; key = r.get("key", "value")
    parts = ask(cmd=r["verb"], **(r.get("args") or {}), binary=True)
    head = json.loads(parts[0])
    if not head.get("ok"):
        raise RuntimeError(f"binary read refused: {head.get('error')}")
    specs = head.get("binary") or []
    if len(specs) != len(parts) - 1:
        raise RuntimeError(f"header lists {len(specs)} part(s), {len(parts) - 1} arrived")
    spec = next((x for x in specs if x.get("key") == key), None)
    if spec is None:
        raise RuntimeError(f"no binary part named {key!r}")
    raw = parts[1 + specs.index(spec)]
    dt = np.dtype(spec["dtype"]); shape = tuple(spec["shape"])
    if len(raw) != dt.itemsize * int(np.prod(shape)):
        raise RuntimeError(f"{len(raw)} bytes for {dt} x {list(shape)}")
    a = np.frombuffer(raw, dt).reshape(shape)
    want = tuple(x.get("length") for x in d.get("dims") or [])
    if all(want) and want != shape:
        raise RuntimeError(f"shape {list(shape)}, describe's dims say {list(want)}")
    if d.get("max") is not None and a.size and a.max() > d["max"]:
        raise RuntimeError(f"a value {a.max()} above the declared max {d['max']}")
    plain = ask(cmd=r["verb"], **(r.get("args") or {}))
    if len(plain) != 1:
        raise RuntimeError("a request WITHOUT binary got a multipart reply")
    pj = json.loads(plain[0])
    v = pj.get(key)
    if isinstance(v, dict) and "b64" in v:
        b = np.frombuffer(base64.b64decode(v["b64"]), np.dtype(v["dtype"])).reshape(v["shape"])
        if not np.array_equal(a, b):
            raise RuntimeError("the JSON reply holds another value than the binary one")
    out = {"ok": True, "why": f"{dt} x {list(shape)}, {len(raw)} bytes"}
except zmq.Again:
    out["why"] = "no reply"
except Exception as exc:
    out["why"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""

# Three `status` replies ~0.4 s apart (a value may only appear once the
# brain's worker has run): prints a JSON list of status dicts, or null.
_STATUS = r"""
import json, sys, time, zmq
s = zmq.Context.instance().socket(zmq.REQ)
s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, 3000)
s.connect(f"tcp://127.0.0.1:{sys.argv[1]}")
out = []
try:
    for _ in range(3):
        s.send_json({"cmd": "status"}); r = s.recv_json()
        if r.get("ok"):
            out.append(r.get("status") or {})
        time.sleep(0.4)
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
    control.py and secure.py are REQUIRED since every module has them
    (control 2026-10-03, encryption 2026-10-04): a module without one would
    ignore the control lock, or be unreachable once the lab's policy secures
    it. control_bar.py only where the module has a GUI (zpiezo has none).

    A SUITE PROJECT (scan-core's scan server) carries no copies: it depends on
    suite-common and imports the masters themselves -- checked here by text."""
    if m.suite_project:
        text = ""
        for f in m.dir.rglob("*.py"):
            if ".venv" in f.parts or "tests" in f.parts:
                continue
            try:
                text += f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        for mod in ("control", "secure"):
            ok = f"suite_common.{mod}" in text or f"from suite_common import {mod}" in text
            rep.add(m.key, f"{mod}.py: uses the suite-common master", "PASS" if ok else "FAIL",
                    "a suite project imports suite_common." + mod if ok else
                    f"no import of suite_common.{mod} in {m.dir.name}")
        return
    pkg = package_dir(m.dir)
    if pkg is None:
        return
    for rel, master, required in (("control.py", CONTROL_MASTER, True),
                                  ("apps/control_bar.py", CONTROL_BAR_MASTER, False),
                                  ("secure.py", SECURE_MASTER, True),
                                  ("follow.py", FOLLOW_MASTER, False),
                                  ("softramp.py", SOFTRAMP_MASTER, False)):
        copy = pkg / rel
        name = f"{rel} is the master copy"
        if not copy.is_file():
            if required:
                rep.add(m.key, name, "FAIL",
                        f"src/{pkg.name}/{rel} missing: copy suite-common/src/suite_common/"
                        f"{Path(rel).name} and wire it in (INSTRUMENT_MODULE_GUIDE section 6)")
            continue
        if not master.is_file():
            rep.add(m.key, name, "FAIL", f"master {master} missing")
        # compared without line endings: git stores both normalised, but a
        # Windows checkout can hold one as CRLF and the other as LF (found
        # 2026-10-04: 63 false "differs" on identical files)
        elif (copy.read_bytes().replace(b"\r\n", b"\n")
              != master.read_bytes().replace(b"\r\n", b"\n")):
            rep.add(m.key, name, "FAIL",
                    f"src/{pkg.name}/{rel} differs: copy suite-common/src/suite_common/"
                    f"{Path(rel).name}")
        else:
            rep.add(m.key, name, "PASS")


def hwlock_check(rep: Report, m, master: bytes | None):
    """The module's hwlock.py is the master copy, and its real backends claim."""
    if m.suite_project:
        # scan-core's scan server opens no instrument: it is a CLIENT of the
        # modules, and each of them holds its own address lock
        rep.add(m.key, "hwlock.py is the master copy", "SKIP",
                "a suite project: owns no hardware (it drives the modules)")
        return
    pkg = package_dir(m.dir)
    if pkg is None:
        rep.add(m.key, "hwlock.py is the master copy", "FAIL", "no single package under src/")
        return
    copy = pkg / "hwlock.py"
    fix = "copy suite-common/src/suite_common/hwlock.py"
    if not copy.is_file():
        rep.add(m.key, "hwlock.py is the master copy", "FAIL", f"src/{pkg.name}/hwlock.py missing: {fix}")
    elif master is not None and (copy.read_bytes().replace(b"\r\n", b"\n")
                                 != master.replace(b"\r\n", b"\n")):   # CRLF vs LF checkout
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


#: Run a probe with its vendor SDK made MISSING: the usual vendor packages
#: cannot be imported and ctypes cannot load a DLL (TLPMX, sa_api). A probe
#: must still print its JSON line -- with a note saying what to install.
_NO_SDK = r"""
import ctypes, runpy, sys
for name in ("pylablib", "pylablib.devices", "ids_peak", "ids_peak.ids_peak",
             "ids_peak_ipl", "harvesters", "harvesters.core", "nidaqmx",
             "nidaqmx.system", "zhinst", "zhinst.core", "pyvisa", "serial"):
    sys.modules[name] = None
def _no_dll(*a, **k):
    raise OSError("SDK missing (check_modules)")
ctypes.CDLL = ctypes.WinDLL = _no_dll
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def probe_check(rep: Report, m, py: Path):
    """A declared probe prints one valid JSON line, also without its SDK."""
    script = m.dir / m.probe
    for label, cmd in (("as installed", [str(py), str(script)]),
                       ("with its SDK missing", [str(py), "-c", _NO_SDK, str(script)])):
        name = f"probe prints its JSON line ({label})"
        try:
            r = subprocess.run(cmd, cwd=m.dir, capture_output=True, text=True,
                               timeout=60, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            rep.add(m.key, name, "FAIL", "no answer within 60 s")
            continue
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        why = ""
        if r.returncode != 0:
            err = [ln for ln in (r.stderr or "").splitlines() if ln.strip()]
            why = f"exit code {r.returncode}" + (f": {err[-1][:160]}" if err else "")
        elif not lines:
            why = "printed nothing"
        else:
            try:
                data = json.loads(lines[-1])
                if not isinstance(data.get("devices"), list):
                    why = "'devices' is not a list"
                elif not all(isinstance(d, dict) and str(d.get("address", "")).strip()
                             for d in data["devices"]):
                    why = "a device without an address"
                elif not lines[-1].isascii():
                    why = "not ASCII (gotcha #14)"
            except (ValueError, AttributeError) as exc:
                why = f"not JSON ({exc})"
        detail = why
        if not why:
            data = json.loads(lines[-1])
            detail = f"{len(data['devices'])} device(s)" + \
                (f"; note: {data.get('note')[:100]}" if data.get("note") else "")
        rep.add(m.key, name, "FAIL" if why else "PASS", detail)


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

    def n_samples(v):
        # a complex channel travels as {"re": [...], "im": [...]} (a VNA
        # trace: one list per sample inside), anything else as a plain list
        if isinstance(v, dict):
            re, im = v.get("re"), v.get("im")
            if not isinstance(re, list) or not isinstance(im, list) or len(re) != len(im):
                return -1
            return len(re)
        return len(v) if isinstance(v, list) else -1

    if not chunks:
        problems.append("stream_start / stream_read / stream_stop not answered ok")
    else:
        for c in chunks:
            c = c or {}
            t = c.get("t") or []
            vals = c.get("values") or {}
            t_ch = c.get("t_ch") or {}
            # a channel the module cannot stream right now (a VNA's u with no
            # reference) must be named in `errors`, never silently left out
            errors = c.get("errors") or {}
            missing = channels - set(vals) - set(errors)
            if missing:
                problems.append(f"channels missing from the reply: {sorted(missing)}")
            if any(n_samples(v) != len(t_ch.get(k, t)) for k, v in vals.items()):
                problems.append("a channel has a different length than its time stamps")
            if "now" not in c:
                problems.append("no `now` in the reply (clock offset cannot be estimated)")
        if not problems and not any((c or {}).get("t") for c in chunks):
            problems.append("recorded nothing in 0.6 s")
    rep.add(m.key, "live: stream verbs work", "FAIL" if problems else "PASS",
            "; ".join(sorted(set(problems))) if problems
            else f"{len(channels)} channel(s), "
                 f"{sum(len((c or {}).get('t') or []) for c in chunks)} samples in 0.6 s")


def binary_check(rep: Report, m, py: Path, cmd: int, manifest: dict):
    """A detector declared with `read.binary` (a camera image) must really
    come back as a binary reply part that matches its header and describe --
    and still as one JSON part for a client that did not ask for binary."""
    for d in manifest.get("parameters", []):
        r = d.get("read")
        if not isinstance(r, dict) or not r.get("binary"):
            continue
        name = f"live: binary read of '{d.get('id')}' works"
        p = subprocess.run([str(py), "-c", _BINARY, str(cmd), json.dumps(d)],
                           capture_output=True, text=True, timeout=60)
        try:
            res = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            res = {"ok": False, "why": (p.stderr or p.stdout).strip()[-150:]}
        rep.add(m.key, name, "PASS" if res.get("ok") else "FAIL", res.get("why", ""))


def ramp_check(rep: Report, m, py: Path, cmd: int, manifest: dict):
    """A declared `ramp` must work: scan-core flies the knob through it.
    Each one: the block names its verbs and rate, a short sweep starts, its
    number shows up finished in status, the readback stream recorded it, and
    the stop verb answers."""
    for d in manifest.get("parameters", []):
        r = d.get("ramp")
        if not isinstance(r, dict):
            continue
        name = f"live: ramp of '{d.get('id')}' works"
        missing = [k for k in ("start", "stop", "rate") if not r.get(k)]
        args = (r.get("start") or {}).get("args") or {}
        if missing or not args.get("to") or not args.get("rate"):
            rep.add(m.key, name, "FAIL", "ramp block needs start{verb, args{to, rate}}, "
                    "stop{verb} and rate{unit, min, max}")
            continue
        info = {k: d.get(k) for k in ("id", "read_path", "scale", "min", "max")}
        info["ramp"] = r
        res = subprocess.run([str(py), "-c", _RAMP, str(cmd), json.dumps(info)],
                             capture_output=True, text=True, timeout=60)
        try:
            out = json.loads(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {"ok": False, "why": (res.stderr or "no output").strip()[-200:]}
        rep.add(m.key, name, "PASS" if out.get("ok") else "FAIL",
                f"{out.get('samples', 0)} readback samples" if out.get("ok")
                else out.get("why", ""))


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


# Encryption, end to end, with the module's OWN secure.py (argv: the path of
# secure.py, the security folder to create). Makes this "PC"'s key, a keyring
# holding it, and the policy enforce ["*"]. Prints nothing.
_SECURE_SETUP = r"""
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("check_secure", sys.argv[1])
S = importlib.util.module_from_spec(spec); sys.modules[spec.name] = S
spec.loader.exec_module(S)   # registered first: secure.py uses dataclasses
me = Path(sys.argv[2]); kr = me / "keyring"; kr.mkdir(parents=True)
pub, sec = S.new_keypair()
meta = {"pc": "check-pc", "machine": "yes"}
S.write_cert(me / S.OWN_PUBLIC, pub, meta=meta)
S.write_cert(me / S.OWN_SECRET, pub, sec, meta=meta)
S.write_cert(kr / "check-pc.key", pub, meta=meta)
(me / S.SETTINGS_FILE).write_text(json.dumps({"keyring": str(kr)}), encoding="utf-8")
(kr / S.POLICY_FILE).write_text(json.dumps({"mode": "enforce", "modules": ["*"]}),
                                encoding="utf-8")
"""

# argv: secure.py, module key, cmd port, pub port. One JSON line:
# plain describe (must get NO answer), CurveZMQ describe, a CurveZMQ status
# frame on SUB, and the CurveZMQ shutdown reply.
_SECURE_PROBE = r"""
import importlib.util, json, sys, time, zmq
spec = importlib.util.spec_from_file_location("check_secure", sys.argv[1])
S = importlib.util.module_from_spec(spec); sys.modules[spec.name] = S
spec.loader.exec_module(S)   # registered first: secure.py uses dataclasses
key, cmd, pub = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
ctx = zmq.Context.instance()
def ask(req, curve, timeout=3000):
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, timeout)
    s.setsockopt(zmq.SNDTIMEO, timeout)
    if curve:
        assert S.secure_client(s, "127.0.0.1", key)
    s.connect(f"tcp://127.0.0.1:{cmd}")
    try:
        s.send_json(req); return s.recv_json()
    except zmq.Again:
        return None
    finally:
        s.close(0)
out = {}
out["plain"] = ask({"cmd": "describe"}, False, 1500) is not None
d = None
t0 = time.monotonic()
while d is None and time.monotonic() - t0 < 30:
    d = ask({"cmd": "describe"}, True)
out["module"] = (d or {}).get("describe", {}).get("module")
sub = ctx.socket(zmq.SUB); sub.setsockopt(zmq.LINGER, 0); sub.setsockopt(zmq.RCVTIMEO, 5000)
S.secure_client(sub, "127.0.0.1", key)
sub.connect(f"tcp://127.0.0.1:{pub}"); sub.setsockopt(zmq.SUBSCRIBE, b"status")
try:
    out["status_frame"] = sub.recv_multipart()[0] == b"status"
except zmq.Again:
    out["status_frame"] = False
sub.close(0)
r = ask({"cmd": "shutdown"}, True)
out["shutdown"] = bool(r and r.get("ok"))
print(json.dumps(out))
"""


def secure_live_check(rep: Report, m, py: Path):
    """A module with src/<pkg>/secure.py must really speak CurveZMQ when the
    lab's policy secures it: started with a throw-away keyring in 'enforce',
    a plain client gets NO answer, a keyed client gets describe and the
    status stream, and shutdown (encrypted) stops it. One generic check
    instead of a security test file per module."""
    import tempfile
    pkg = package_dir(m.dir)
    sec = pkg / "secure.py" if pkg is not None else None
    if m.suite_project:
        sec = SECURE_MASTER         # it imports the master (no copy of its own)
    if sec is None or not sec.is_file():
        return
    name = "live: encrypted (CurveZMQ) when the policy secures it"
    me = Path(tempfile.mkdtemp(prefix="aaltoflow-check-sec-")) / "pc"
    r = subprocess.run([str(py), "-c", _SECURE_SETUP, str(sec), str(me)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        rep.add(m.key, name, "FAIL", f"key setup failed: {(r.stderr or '').strip()[-150:]}")
        return
    env = dict(os.environ, AALTOFLOW_SECURITY_DIR=str(me))
    cmd, pub = free_port_pair()
    proc = subprocess.Popen([str(py), m.service, "--cmd-port", str(cmd), "--pub-port", str(pub)],
                            cwd=m.dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env)
    try:
        r = subprocess.run([str(py), "-c", _SECURE_PROBE, str(sec), m.key, str(cmd), str(pub)],
                           capture_output=True, text=True, timeout=90, env=env)
        try:
            res = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            res = None
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            pass
        exited = proc.poll() is not None
        out = proc.stdout.read() if exited else ""
        problems = []
        if res is None:
            problems.append(f"probe failed: {(r.stderr or r.stdout).strip()[-150:]}")
        else:
            if res["plain"]:
                problems.append("a PLAIN client was answered")
            if res["module"] != m.key:
                problems.append(f"keyed describe: {res['module']!r}")
            if not res["status_frame"]:
                problems.append("no encrypted status frame on SUB")
            if not res["shutdown"]:
                problems.append("encrypted shutdown not accepted")
        if not exited:
            problems.append("still running 15 s after shutdown")
        elif f"security: {m.key} is encrypted" not in out:
            problems.append("console has no 'security: ... is encrypted' line")
        rep.add(m.key, name, "FAIL" if problems else "PASS", "; ".join(problems))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def _at(status, path):
    """The value at `read_path` in a status dict, or KeyError."""
    v = status
    for k in path:
        if isinstance(v, list):
            v = v[int(k)]
        else:
            v = v[k]
    return v


def _type_problem(d: dict, v) -> str:
    """Why value `v` does not fit descriptor `d`'s declared type, or "".

    The same promise scan-core keeps when it STORES a value (scan_core/
    storage.py, developer notes 4b): a bool is True/False, an int a whole
    number inside the INDICATOR's min/max or bits, an enum one of its options.
    None means "no value now" and always fits. A control's min/max are
    setting limits, not checked here (scan-core does not narrow them either)."""
    if v is None:
        return ""
    t = d.get("type")
    items = v if (isinstance(v, list) and d.get("dtype") != "complex") else [v]
    for x in items:
        if x is None:
            continue
        if t == "bool" and not isinstance(x, bool) and x not in (0, 1):
            return f"declared bool, reads {x!r}"
        if t == "int":
            if isinstance(x, bool) or not isinstance(x, (int, float)) or x != int(x):
                return f"declared int, reads {x!r}"
            if d.get("kind") == "indicator":
                lo, hi = d.get("min"), d.get("max")
                if d.get("bits") is not None:
                    lo, hi = 0, 2 ** int(d["bits"]) - 1
                if (lo is not None and x < lo) or (hi is not None and x > hi):
                    return f"reads {x!r}, outside its declared [{lo}, {hi}]"
        if t == "enum":
            opts = d.get("options") or []
            if x not in opts and str(x) not in [str(o) for o in opts]:
                return f"reads {x!r}, which is not one of its options {opts}"
        if t == "float" and (isinstance(x, (str, bool)) or not isinstance(x, (int, float))):
            if not (isinstance(x, dict) and {"re", "im"} <= set(x)):
                return f"declared float, reads {x!r}"
    return ""


def types_check(rep: Report, m, py: Path, cmd: int, manifest: dict):
    """Every value status reports fits the type its describe declares
    (scan-core stores each detector in that type since 2026-10-04: a wrong
    int range would stop a scan, an enum value outside its options is lost).
    Three status snapshots of the simulator -- a first line of defence; the
    real instrument may report more."""
    r = subprocess.run([str(py), "-c", _STATUS, str(cmd)], capture_output=True,
                       text=True, timeout=30)
    try:
        snaps = json.loads(r.stdout.strip().splitlines()[-1]) or []
    except (ValueError, IndexError):
        snaps = []
    if not snaps:
        rep.add(m.key, "live: status values fit their declared types", "SKIP",
                "no status reply")
        return
    problems = []
    for d in manifest.get("parameters", []):
        path = d.get("read_path")
        if not path or d.get("kind") not in ("indicator", "control"):
            continue
        for st in snaps:
            try:
                v = _at(st, path)
            except (KeyError, IndexError, TypeError, ValueError):
                break
            why = _type_problem(d, v)
            if why:
                problems.append(f"{d.get('id')}: {why}")
                break
    rep.add(m.key, "live: status values fit their declared types",
            "FAIL" if problems else "PASS", "; ".join(problems[:6]) +
            (f" (+{len(problems) - 6} more)" if len(problems) > 6 else ""))


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
        ramp_check(rep, m, py, cmd, manifest)
        binary_check(rep, m, py, cmd, manifest)
        types_check(rep, m, py, cmd, manifest)
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
        if m.suite_project:
            # deliberate: the scan server IS scan-core (one environment, one
            # lock file); suite_common.modules.SUITE_PROJECTS
            rep.add(m.key, "sits in modules/<category>/", "PASS",
                    f"{where}: a suite project, stays in the root on purpose")
        elif is_legacy_location(args.root, m.dir):
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
        if m.address_arg:
            # module.toml [hardware]: the launcher passes the address chosen in
            # "Instruments on this PC" with this flag -- it must exist
            ok = m.address_arg in text
            rep.add(m.key, f"run_service accepts {m.address_arg} ([hardware])",
                    "PASS" if ok else "FAIL",
                    "" if ok else f"module.toml names {m.address_arg}, the script does not take it")
        if m.probe:
            # module.toml [hardware] probe: Mission Control runs it to LIST
            # the devices the module's vendor library sees
            probe_check(rep, m, py)
        if m.gui:
            text = help_text(py, m.dir, m.gui)
            miss = [f for f in ("--connect", "--cmd-port", "--pub-port") if f not in text]
            rep.add(m.key, "run_gui accepts --connect --cmd-port --pub-port",
                    "FAIL" if miss else "PASS", "missing " + ", ".join(miss) if miss else "")
        if args.live:
            live_check(rep, m, py)
            port_taken_check(rep, m, py)
            secure_live_check(rep, m, py)

    rep.print()
    n_fail = len(rep.failed)
    n_warn = len(rep.warned)
    print(f"\n{len(rep.rows)} checks, {n_fail} failed"
          + (f", {n_warn} warning(s)" if n_warn else ""))
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
