"""scan_server.py -- the scan engine as a SERVICE: start a scan on the lab PC,
watch it (and abort it, answer its questions) from any PC.

Why (Lukas, 2026-10-05): "you can connect to a gui of any running instrument
as this is a service... but scan core runs on a single computer so this cannot
be controlled remotely.... I would like to set a measurement on a lab pc but
then observe/set on my office pc". Until now the scan engine ran INSIDE the
measurement suite's window: close the window and the scan stops; sit at
another PC and you see nothing. The scan server moves the engine into its own
process -- a service with exactly the wire contract every instrument module
has (docs/DEVELOPER_NOTES.md section 4) -- and every measurement suite, on the
lab PC or in the office, becomes a CLIENT of it.

PHASE 1 (this file): WATCH. A suite on the server's own PC submits a scan
(or a queue); any suite can watch it live -- progress, ETA, where it is, the
live map, the log, the PAUSED fault banner, the operator's pause question --
and Abort / Continue / Abort all / Stop queue. The scan does not depend on any
window: a suite that closes (or crashes, or loses its network) changes nothing.
PHASE 2 (not yet): defining and submitting scans from another PC, and editing
a running queue (docs/DEVELOPER_NOTES.md, "The scan server").

What it is made of -- nothing new where something tested exists:
  * the ENGINE: scan_core.engine.run, called exactly as the suite's ScanWorker
    calls it (abort, faults pause, operator pause, live snapshots);
  * the FILES: scan_core.autosave -- the same <data dir>/<date>/<time>_<name>.nc,
    the same checkpoints (every 1/10 of a scan over 100 points), the same
    atomic writes; the run info a suite sends goes into the attributes, and
    the engine adds provenance (setup_name of THIS PC) and the snapshot;
  * the INSTRUMENTS: the suite's "follow the launcher" on this PC -- module
    discovery + probe, connect whatever runs, reconnect when that changes
    and no scan runs (scan_core.lab.build_lab_registry);
  * the WIRE: REQ/REP JSON + PUB (status ~2 Hz, event per log line, and a
    small `live` notice when new data is there -- the data itself is fetched
    with `get_live`, so big payloads stay off the PUB socket); control
    (suite_common.control: one controller, many viewers) and encryption
    (suite_common.secure, policy key "scanserver") imported from suite-common
    itself -- scan-core depends on it, so no copies are needed.

Control (who may do what):
  * watching -- status, info, describe, get_config, get_live, get_log -- is
    free for everyone;
  * `abort` and `stop_queue` are SAFETY verbs: always allowed, for any client
    on any PC (like a stage's `stop`): whoever sees a scan going wrong must be
    able to stop it, and above all the PC that started it must never be locked
    out of its own Abort by somebody else holding control;
  * `submit`, `submit_queue`, `answer_pause`, `clear_fault`, `set_config`
    need control when somebody holds it (nobody holds it -> allowed, as for
    every module). A person's suite takes control with the "Take control"
    button of the watch header;
  * PHASE 1: `submit` / `submit_queue` only from a client on THIS PC. "This
    PC" = the PC part of the request's client identity ("user@PC",
    suite_common.control.pc_of) equals this PC's name. With encryption on, the
    service's Guard has already checked that name against the CurveZMQ key
    that sent the request (secure.Guard.check); with it off it is self-declared
    -- like everything control does, a guard against mistakes, not security.
  * `shutdown` (the launcher's Stop) while a scan runs ABORTS the scan and the
    queue, waits for the after-scan routine and the final save, then exits.
    A refusal would not help: Mission Control kills a service that refuses,
    and a kill would lose the after-scan routine ("field -> 0") and the save.

Run it: scripts/run_scan_server.py (Mission Control's "Scan server" card).
No Qt in here: it runs headless, and is tested without a screen.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import socket
import tempfile
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

import numpy as np
import zmq

from suite_common import secure
from suite_common.control import ControlLease, describe_holder, pc_of

from . import autosave
from .engine import run
from .errors import RoutineError, ScanAborted, ScanFault
from .recipe import Recipe

#: The module key: describe's "module", module.toml's key, the policy name.
SERVER_KEY = "scanserver"
#: The next free pair of the suite's port scheme on 2026-10-05.
DEFAULT_CMD_PORT = 5551
DEFAULT_PUB_PORT = 5552

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"
#: "new data is there" -- {"live_rev": n}; clients then call get_live
TOPIC_LIVE = b"live"

#: Status frames per second. A watching suite redraws at this rate; a scan
#: point takes far longer than half a second anyway.
STATUS_HZ = 2.0
#: Seconds between live snapshots (building one costs; nobody needs more).
LIVE_EVERY_S = 1.0
#: Scans shorter than this are only saved at the end (the suite's rule).
CHECKPOINT_ABOVE = 100
#: How often the instrument set is re-checked while idle (s).
FOLLOW_EVERY_S = 3.0
#: Log lines kept in memory (a client fetches them with get_log).
LOG_KEEP = 5000
#: Log lines in every status frame.
LOG_TAIL = 15
#: How a live dataset travels: netCDF file bytes, zlib, base64 (JSON-safe).
LIVE_ENCODING = "netcdf+zlib+base64"

STATES = ("idle", "running", "paused", "waiting_operator")


class PortInUse(RuntimeError):
    """A port is taken (another scan server, an orphan -- gotcha #7). Raised
    by start() before anything else happens; the script exits with code 2."""


# ───────────────────────── the live dataset on the wire ──────────────────────

def dataset_to_text(ds) -> str:
    """A dataset as JSON-safe text: netCDF bytes, zlib-compressed, base64.

    netCDF because it is what the files are -- the client gets EXACTLY what a
    checkpoint would contain (attributes, the recipe, the complex _real/_imag
    split) and opens it with the same code. Written through a temporary file:
    the h5netcdf engine is the one the suite writes with, and it wants a path.
    """
    fd, tmp = tempfile.mkstemp(suffix=".nc", prefix="scanserver-live-")
    os.close(fd)
    try:
        ds.to_netcdf(tmp)
        raw = Path(tmp).read_bytes()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return base64.b64encode(zlib.compress(raw, 6)).decode("ascii")


def dataset_from_text(text: str):
    """The inverse of dataset_to_text: an xarray.Dataset, fully in memory."""
    import xarray as xr
    raw = zlib.decompress(base64.b64decode(text.encode("ascii")))
    fd, tmp = tempfile.mkstemp(suffix=".nc", prefix="scanserver-live-")
    os.close(fd)
    try:
        Path(tmp).write_bytes(raw)
        # load() + close: the temporary file must be deletable on Windows
        with xr.open_dataset(tmp) as ds:
            out = ds.load()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return out


# ──────────────────────────────── the status line ─────────────────────────────

def fmt_duration(seconds) -> str:
    """'1h 05m', '3m 20s', '12s' -- the suite's format."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def where_text(where, done, total, eta_s, queue_i=-1, queue_n=0, now="") -> str:
    """WHERE a running scan is, in one line -- the same line the suite's
    Measurement header shows for a scan of its own (ScanBuilder.run_status_text):

        scan 2 of 3   point 25 / 125   field 40 mT (5/9)   ~3m 20s left   now: ...
    """
    bits = []
    if queue_n > 1 and queue_i >= 0:
        bits.append(f"scan {queue_i + 1} of {queue_n}")
    bits.append(f"point {done:,} / {total:,}" if total else "starting")
    where = where or {}
    if where.get("row"):
        bits.append("row {} / {}".format(*where["row"]))
    for a in where.get("axes", ()):
        if a.get("value") is None:
            continue                    # a fly axis: the whole row at once
        unit = f" {a['unit']}" if a.get("unit") else ""
        bits.append(f"{a['name']} {a['value']:g}{unit} ({a['i'] + 1}/{a['n']})")
    if total and done < total and eta_s is not None:
        bits.append(f"~{fmt_duration(eta_s)} left")
    if now:
        bits.append(f"now: {now}")
    return "   ".join(bits)


def _routine_step(msg: str):
    """'<when>: <step> ...' -> '<step> (<when>)'; '' when a step ends; None
    for any other line (the suite's ScanBuilder._routine_step)."""
    label, sep, what = msg.partition(": ")
    if not sep:
        return None
    if what.endswith(" ..."):
        return f"{what[:-4]} ({label})"
    if what.endswith((" done", "carrying on", "(aborted)")):
        return ""
    return None


def _jsonable(obj):
    """numpy numbers and tuples in a status -> plain JSON."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


# ────────────────────────────────── describe ──────────────────────────────────

def _p(id, label, kind, type, read_path=None, **extra):
    d = {"id": id, "label": label, "kind": kind, "type": type,
         "unit": extra.pop("unit", ""), "group": extra.pop("group", "Scan"),
         "order": extra.pop("order", 0), "writable": False,
         "plottable": extra.pop("plottable", False), "read_path": read_path}
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


def build_manifest() -> dict:
    """What a generic client (Mission Control's Variables, the contract check)
    sees. The scan server is NOT an instrument: scan-core's follow-the-launcher
    never builds parameters from this (suite_common COORDINATOR_KEYS)."""
    params = [
        _p("state", "State", "indicator", "enum", ["state"], order=1,
           options=list(STATES)),
        _p("scan", "Scan", "indicator", "string", ["scan"], order=2),
        _p("done", "Points done", "indicator", "int", ["done"], order=3, min=0),
        _p("total", "Points in the scan", "indicator", "int", ["total"], order=4, min=0),
        _p("progress", "Progress", "indicator", "float", ["progress"], order=5,
           unit="%", plottable=True),
        _p("eta_s", "Time left (scan)", "indicator", "float", ["eta_s"], order=6, unit="s"),
        _p("elapsed_s", "Elapsed", "indicator", "float", ["elapsed_s"], order=7, unit="s"),
        _p("where", "Where", "indicator", "string", ["where"], order=8),
        _p("queue_pos", "Scan of the queue", "indicator", "int", ["queue", "pos"], order=9),
        _p("queue_n", "Scans in the queue", "indicator", "int", ["queue", "n"], order=10, min=0),
        _p("pause_message", "Waiting for the operator", "indicator", "string",
           ["pause_message"], order=11),
        _p("save_path", "Saving to", "indicator", "string", ["save_path"], order=12,
           group="Files"),
        _p("data_dir", "Data folder (server PC)", "indicator", "string", ["data_dir"],
           order=13, group="Files"),
        _p("live_rev", "Live data revision", "indicator", "int", ["live_rev"], order=14,
           min=0),
        # a SAFETY verb: allowed for everyone, always (control.py `safety`)
        _p("abort", "Abort scan", "action", "action", order=90, group="Run",
           danger=True, help="Stop the running scan (a queue goes on with the next "
                             "one). Always allowed, for every PC."),
        _p("stop_queue", "Stop queue", "action", "action", order=91, group="Run",
           danger=True, help="Abort the running scan AND the rest of the queue."),
        _p("answer_pause", "Answer the operator pause", "action", "action", order=92,
           group="Run", args=[{"name": "answer", "type": "enum",
                               "options": ["continue", "abort", "all"],
                               "default": "continue"}]),
        _p("clear_fault", "Clear a module's fault", "action", "action", order=93,
           group="Run", args=[{"name": "module", "type": "string", "default": ""}]),
    ]
    manifest = {"schema": 1, "module": SERVER_KEY, "label": "Scan server",
                "parameters": params}
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"))
    manifest["revision"] = zlib.crc32(blob.encode("utf-8"))
    return manifest


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
#: the largest Navigator design file a suite may share through the server
DESIGN_MAX_BYTES = 32 * 2**20
#: bytes per get_file reply: a big measurement comes in pieces, so no single
#: request holds the server's command thread (and an Abort) for long
FILE_CHUNK = 4 * 2**20


# ──────────────────────────────────── a scan ──────────────────────────────────

class _Entry:
    """One scan of what was submitted (a single scan is a queue of one)."""

    def __init__(self, name: str, recipe: Recipe, attrs: dict):
        self.name = name
        self.recipe = recipe
        self.attrs = attrs
        self.result: str | None = None      # done / aborted / error
        self.path: str = ""
        self.error: str = ""
        self.stop_all = False
        self.stop_reason = ""
        self.n_points = 0                    # set when validated (queue ETA)

    def named_recipe(self) -> Recipe:
        r = Recipe.from_dict(json.loads(self.recipe.to_json()))
        r.name = self.name
        return r


def _clean_attrs(attrs) -> dict:
    """Only {text: text}: attributes go into a netCDF file."""
    if not isinstance(attrs, dict):
        return {}
    return {str(k): str(v) for k, v in attrs.items()
            if isinstance(k, str) and v is not None and str(v) != ""}


# ────────────────────────────────── the service ───────────────────────────────

class ScanServer:
    """The scan server: REQ/REP + PUB, one scan (or queue) at a time.

    registry   : a FIXED registry (the simulator, tests): the instruments are
                 not followed. None = follow the launcher on this PC.
    root       : the suite root whose discovery and settings are used.
    data_dir   : where files go; None = the PC's suite setting (data_dir) at
                 the moment a scan is submitted, else scan-core/out.
    """

    def __init__(self, host: str = "0.0.0.0", cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT, status_hz: float = STATUS_HZ,
                 registry=None, root=None, data_dir=None,
                 follow_every_s: float = FOLLOW_EVERY_S,
                 live_every_s: float = LIVE_EVERY_S, echo=True):
        self.host, self.cmd_port, self.pub_port = host, int(cmd_port), int(pub_port)
        self.status_hz = float(status_hz)
        self.root = Path(root) if root else None
        self.data_dir = Path(data_dir) if data_dir else None
        self.follow = registry is None
        self.follow_every_s = float(follow_every_s)
        self.live_every_s = float(live_every_s)
        self.echo = echo
        #: this PC's name, as control.py's pc_of() writes it ("user@PC" -> "pc")
        self.pc = socket.gethostname().strip().lower()

        self._ctx = zmq.Context.instance()
        self._guard = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._out: "queue.Queue[tuple[bytes, dict]]" = queue.Queue()
        self._lock = threading.RLock()
        self._follow_lock = threading.Lock()

        # instruments
        from .registry import Registry
        self.registry = registry if registry is not None else Registry()
        self.lab = None
        self.connected: list[str] = []
        #: {slug: {"host", "cmd", "pub"}} of the connected instruments; host ""
        #: = this PC. A watcher on another PC connects its own Control tab to
        #: the SAME services under the SAME names (Lukas 2026-10-06: the office
        #: suite looked "very different" from the lab's)
        self.instruments: dict = {}
        self._connected_key: frozenset | None = None
        self._failed_key: frozenset | None = None

        # what runs
        self._busy = False                   # a queue is running (or starting)
        self._runner: threading.Thread | None = None
        self._entries: list[_Entry] = []
        self._qi = -1
        self._abort = False                  # the CURRENT scan
        self._stop_reason = ""               # set = no further scan of the queue
        self._started_by = ""
        self._t_queue = None
        self._reset_scan_state()
        self._last_error = ""
        self._last_summary = ""
        self._shutting_down = False

        # live data and log
        self._live_ds = None
        self._live_rev = 0
        self._live_blob: tuple[int, str] | None = None
        self._log: list[str] = []
        self._log_base = 0                   # index of self._log[0]

        # the scan as SUBMITTED (phase 2 of watching, Lukas 2026-10-06: "a 1:1
        # copy of what i see on the lab pc"): a watcher fetches the definitions
        # and run info with get_scan when scan_rev moves, and the plot choice of
        # the suite on this PC with get_view when view_rev moves
        self._scan_rev = 0
        self._view: dict = {}
        self._view_rev = 0
        self._view_by = ""
        # the Navigator's design FILE of the suite on this PC (it exists only
        # here): held in memory for watchers, replaced by the next one
        self._design: dict | None = None       # {name, kind, cell, width_um, data}
        self._design_rev = 0
        # the data folder's catalogue, brought up to date in a thread of its
        # own: the first indexing of a big folder takes a while, and this
        # server answers every request -- an Abort above all -- from ONE thread
        self._indexer: threading.Thread | None = None

        self.control = ControlLease(
            safety={"abort", "stop_queue"},
            # set_view changes nothing on an instrument: not behind the control
            # lease, but only from this PC (_set_view)
            read={"set_view", "set_design"},
            on_event=lambda level, msg: self.log(msg, level))

    def _reset_scan_state(self):
        # caller holds the lock (or nothing runs yet)
        self._done, self._total, self._eta = 0, 0, None
        self._where = None
        self._now = ""
        self._t_scan = None
        self._faults: list = []
        self._ask = None                     # (message, answer) while waiting
        self._save_path = ""
        self._last_saved = ""
        self._save_error = ""
        self._ck_every = 0
        self._ck_next = 0
        self._last_live = 0.0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Bind both ports (or raise PortInUse), then start the threads."""
        cmd_addr = f"tcp://{self.host}:{self.cmd_port}"
        pub_addr = f"tcp://{self.host}:{self.pub_port}"
        self._pub_sock = self._ctx.socket(zmq.PUB)
        self._rep_sock = self._ctx.socket(zmq.REP)
        try:
            # encryption: when the lab's policy secures "scanserver", both
            # sockets become CurveZMQ servers -- before bind (secure.py)
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], SERVER_KEY,
                on_event=lambda level, msg: self.log(msg, level))
        except secure.SecurityError:
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            raise
        try:
            self._pub_sock.bind(pub_addr)
            self._rep_sock.bind(cmd_addr)
        except zmq.ZMQError as exc:
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            secure.release_server(self._guard)
            self._guard = None
            raise PortInUse(f"cannot listen on {cmd_addr} / {pub_addr} ({exc}); "
                            f"is another scan server already using these ports?") from exc
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._publisher, name="scanserver-pub", daemon=True),
            threading.Thread(target=self._commander, name="scanserver-cmd", daemon=True),
        ]
        if self.follow:
            self._threads.append(threading.Thread(target=self._follower,
                                                  name="scanserver-follow", daemon=True))
        for t in self._threads:
            t.start()
        self.log(f"scan server ready on {cmd_addr} (cmd) / {pub_addr} (pub), "
                 f"PC '{self.pc}'" + ("" if self.follow else " -- fixed registry"))

    def stop(self) -> None:
        """Stop (aborting a running scan first) and release everything."""
        self._abort_all("the scan server stopped")
        r = self._runner
        if r is not None and r.is_alive():
            r.join(timeout=30)
        self._stop.set()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=3.0)
        secure.release_server(self._guard)
        self._guard = None
        with self._lock:
            lab, self.lab = self.lab, None
        if lab is not None:
            try:
                lab.close()
            except Exception:
                pass

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    @property
    def running(self) -> bool:
        return self._busy

    # ------------------------------------------------------------------ #
    # the log
    # ------------------------------------------------------------------ #
    def log(self, msg: str, level: str = "info") -> None:
        """One line of what a GUI would show: kept (get_log), published as an
        `event`, and printed (the launcher's log shows the service console)."""
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        # a routine step starting / ending ("before_scan: set field ... done")
        # becomes the status line's "now: ..." (the suite's _on_log does this)
        step = _routine_step(msg) if isinstance(msg, str) else None
        with self._lock:
            if step is not None:
                self._now = step
            self._log.append(line)
            n = self._log_base + len(self._log) - 1
            if len(self._log) > LOG_KEEP:
                drop = len(self._log) - LOG_KEEP
                del self._log[:drop]
                self._log_base += drop
        self._out.put((TOPIC_EVENT, {"level": level, "msg": msg, "n": n}))
        if self.echo:
            try:   # ASCII, flushed (gotchas #14, #19)
                print(line.encode("ascii", "replace").decode("ascii"), flush=True)
            except Exception:
                pass

    def get_log(self, since: int = 0) -> dict:
        with self._lock:
            start = max(int(since or 0), self._log_base)
            lines = self._log[start - self._log_base:]
            return {"lines": list(lines), "first": start,
                    "next": self._log_base + len(self._log)}

    # ------------------------------------------------------------------ #
    # following the launcher (instruments on THIS PC)
    # ------------------------------------------------------------------ #
    def _follower(self):
        first = True
        while not self._stop.wait(0.0 if first else self.follow_every_s):
            first = False
            try:
                self.follow_once()
            except Exception as exc:          # never let the thread die
                self.log(f"following the launcher failed: {exc}", "warn")

    def _wanted(self) -> dict:
        """{slug: (host, cmd, pub)} of the INSTRUMENT modules that answer now."""
        from concurrent.futures import ThreadPoolExecutor

        from suite_common import discover, probe
        found = discover(self.root)
        mods = [m for m in found.modules if m.is_instrument]
        if not mods:
            return {}
        with ThreadPoolExecutor(max_workers=min(16, len(mods))) as pool:
            ups = list(pool.map(lambda m: probe(m.host, m.cmd, 0.3), mods))
        self._names = {m.slug: m.name for m in mods}
        return {m.slug: (m.host, m.cmd, m.pub) for m, up in zip(mods, ups) if up}

    def follow_once(self, force: bool = False) -> bool:
        """Connect to the instruments running now, if that set changed and no
        scan runs. True when the registry was rebuilt."""
        if not self.follow or self._busy:
            return False
        # the follower thread and a submit (forced re-follow) must not both
        # build a Lab at the same moment: one would leak its connections
        with self._follow_lock:
            return self._follow_locked(force)

    def _follow_locked(self, force: bool) -> bool:
        wanted = self._wanted()
        key = frozenset(wanted.items())
        if not force and (key == self._connected_key or key == self._failed_key):
            return False
        reg, lab, good, bad = self._connect(wanted)
        with self._lock:
            if self._busy:                    # a scan started meanwhile: next time
                if lab is not None:
                    lab.close()
                return False
            old, self.lab = self.lab, lab
            self.registry = reg
            self.connected = sorted(good)
            self.instruments = {
                n: {"host": "" if str(wanted[n][0]).lower() in _LOCAL_HOSTS else wanted[n][0],
                    "cmd": int(wanted[n][1]), "pub": int(wanted[n][2]),
                    "name": getattr(self, "_names", {}).get(n, n)}
                for n in good}
            self._connected_key = key
            self._failed_key = key if bad else None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        if good:
            self.log(f"connected: {', '.join(sorted(good))} -- {len(reg.settables())} "
                     f"settables, {len(reg.gettables())} detectors, "
                     f"{len(reg.actions())} actions")
        else:
            self.log("no instrument module is running on this PC")
        for name, why in bad:
            self.log(f"could not connect {name}: {why}", "warn")
        return True

    def _connect(self, wanted: dict):
        """(registry, lab, good names, [(bad name, why)]). One service that
        answers TCP but fails `describe` must not take the others down: on a
        failure every module is tried alone and the good ones are connected."""
        from .lab import build_lab_registry
        from .registry import Registry
        if not wanted:
            return Registry(), None, [], []

        def build(names):
            reg, lab = build_lab_registry(include=tuple(names),
                                          endpoints={n: wanted[n] for n in names},
                                          prefix=True,
                                          on_warn=lambda m: self.log(f"note: {m}"))
            reg.settings_root = self.root     # snapshot_include_idn lives there
            return reg, lab

        try:
            reg, lab = build(list(wanted))
            return reg, lab, list(wanted), []
        except Exception:
            pass
        good, bad = [], []
        for n in wanted:
            try:
                _r, lab1 = build([n])
                lab1.close()
                good.append(n)
            except Exception as exc:
                bad.append((n, str(exc)))
        if not good:
            return Registry(), None, [], bad
        reg, lab = build(good)
        return reg, lab, good, bad

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #
    def _data_dir(self) -> Path:
        return self.data_dir or autosave.default_data_dir(self.root)

    def _state(self) -> str:
        if not self._busy:
            return "idle"
        if self._ask is not None:
            return "waiting_operator"
        if self._faults:
            return "paused"
        return "running"

    def _fault_rows(self) -> list:
        rows = []
        for f in self._faults:
            name, msg = f[0], f[1]
            can = False
            lab = self.lab
            if lab is not None:
                try:
                    can = bool(lab.can_clear_fault(name))
                except Exception:
                    can = False
            rows.append({"module": name, "message": msg, "can_clear": can})
        return rows

    def status_payload(self) -> dict:
        """The status, built in ONE place (status verb and PUB frame alike)."""
        from suite_common import setup_name
        with self._lock:
            now = time.monotonic()
            entries = self._entries
            e = entries[self._qi] if 0 <= self._qi < len(entries) else None
            pct = 100.0 * self._done / self._total if self._total else 0.0
            # the rest of the queue at the pace measured so far in this scan
            q_eta = None
            if self._busy and self._done and self._t_scan is not None:
                per_pt = (now - self._t_scan) / self._done
                rest = sum(x.n_points for x in entries[self._qi + 1:])
                q_eta = (self._eta or 0.0) + rest * per_pt
            st = {
                "state": self._state(),
                "busy": self._busy,
                "scan": e.name if (e is not None and self._busy) else "",
                "started_by": self._started_by,
                "queue": {"pos": self._qi + 1 if self._busy else 0,
                          "n": len(entries) if self._busy else 0,
                          "names": [x.name for x in entries],
                          "results": [[x.name, x.result, x.path] for x in entries
                                      if x.result is not None],
                          "stop_reason": self._stop_reason,
                          "summary": self._last_summary},
                "done": int(self._done), "total": int(self._total),
                "progress": round(pct, 2),
                "eta_s": None if self._eta is None else round(float(self._eta), 1),
                "queue_eta_s": None if q_eta is None else round(float(q_eta), 1),
                "elapsed_s": round(now - self._t_queue, 1) if (self._busy and self._t_queue)
                else 0.0,
                "scan_elapsed_s": round(now - self._t_scan, 1) if (self._busy and self._t_scan)
                else 0.0,
                "where": where_text(self._where, self._done, self._total, self._eta,
                                    self._qi, len(entries), self._now) if self._busy else "",
                "where_axes": self._where,
                "now": self._now,
                "faults": self._fault_rows() if self._faults else [],
                "pause_message": (self._ask[0] or "(no message)") if self._ask else "",
                "save_path": self._save_path,
                "last_saved": self._last_saved,
                "save_error": self._save_error,
                "live_rev": self._live_rev,
                "log_tail": self._log[-LOG_TAIL:],
                "log_n": self._log_base + len(self._log),
                "error": self._last_error,
                "pc": self.pc,
                "setup_name": setup_name(self.root),
                "data_dir": str(self._data_dir()),
                "modules": list(self.connected),
                "follow": self.follow,
                "phase": 1,
                "scan_rev": self._scan_rev,
                "view_rev": self._view_rev,
                "instruments": dict(self.instruments),
                "design_rev": self._design_rev,
            }
        st["describe_rev"] = build_manifest()["revision"]
        st["control"] = self.control.status()
        return _jsonable(st)

    # ------------------------------------------------------------------ #
    # threads: publisher and commander (one socket each)
    # ------------------------------------------------------------------ #
    def _publisher(self):
        sock = self._pub_sock
        period = 1.0 / self.status_hz
        next_status = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    while True:
                        topic, payload = self._out.get_nowait()
                        sock.send_multipart([topic, _json(payload)])
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now >= next_status:
                    next_status = now + period
                    try:
                        sock.send_multipart([TOPIC_STATUS, _json(self.status_payload())])
                    except Exception:
                        pass                  # status must never take the publisher down
                time.sleep(0.01)
        finally:
            sock.close(0)

    def _commander(self):
        sock = self._rep_sock
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if not dict(poller.poll(200)):
                    continue
                try:
                    frame = sock.recv(copy=False)
                    raw = frame.bytes
                except Exception:
                    continue
                # A REP socket that received MUST answer, whatever came in
                # (gotcha #39): a bad message gets an error reply.
                try:
                    req = json.loads(raw.decode("utf-8"))
                    refused = None
                    if self._guard is not None and isinstance(req, dict):
                        refused = self._guard.check(req, secure.user_id(frame))
                    reply = refused or (self._dispatch(req) if isinstance(req, dict) else
                                        {"ok": False, "error": "request must be a JSON object"})
                except Exception as exc:      # never die on a bad command
                    reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                try:
                    data = _json(reply)
                except Exception as exc:
                    data = _json({"ok": False, "error": f"reply not JSON-encodable: {exc}"})
                try:
                    sock.send(data)
                except Exception:
                    pass
        finally:
            sock.close(linger=500)            # the shutdown reply must get out

    # ------------------------------------------------------------------ #
    # the verbs
    # ------------------------------------------------------------------ #
    def _dispatch(self, req: dict) -> dict:
        gate = self.control.handle(req)
        if gate is not None:
            return gate
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, "status": self.status_payload()}
        if cmd == "describe":
            return {"ok": True, "describe": build_manifest()}
        if cmd == "info":
            from suite_common import setup_name
            try:
                from importlib.metadata import version
                ver = version("scan-core")
            except Exception:
                ver = ""
            return {"ok": True, "info": {
                "idn": f"AaltoFlow scan server (scan-core {ver})".replace(" ()", ""),
                "pc": self.pc, "setup_name": setup_name(self.root),
                "data_dir": str(self._data_dir()), "modules": list(self.connected),
                "phase": 1, "follow": self.follow}}
        if cmd == "get_config":
            return {"ok": True, "config": self._config()}
        if cmd == "set_config":
            return self._set_config(req.get("config") or {})
        if cmd == "shutdown":
            return self._shutdown(bool(req.get("force", False)))
        if cmd == "get_log":
            return {"ok": True, **self.get_log(req.get("since", 0))}
        if cmd == "get_live":
            return self._get_live(req.get("have_rev"))
        if cmd == "submit":
            entry = {"name": req.get("name"), "recipe": req.get("recipe"),
                     "attrs": req.get("attrs")}
            return self._submit(req, [entry], req.get("attrs"),
                                bool(req.get("allow_unsaved", False)))
        if cmd == "submit_queue":
            return self._submit(req, req.get("entries"), req.get("attrs"),
                                bool(req.get("allow_unsaved", False)))
        if cmd == "abort":
            return self._abort_verb(req)
        if cmd == "stop_queue":
            return self._stop_queue_verb(req)
        if cmd == "answer_pause":
            return self._answer(req.get("answer", True), req)
        if cmd == "clear_fault":
            return self._clear_fault(str(req.get("module") or ""), req)
        if cmd == "get_scan":
            return self._get_scan()
        if cmd == "get_view":
            with self._lock:
                return {"ok": True, "view": dict(self._view), "view_rev": self._view_rev,
                        "by": self._view_by}
        if cmd == "set_view":
            return self._set_view(req)
        if cmd == "set_design":
            return self._set_design(req)
        if cmd == "get_design":
            with self._lock:
                d = dict(self._design) if self._design else None
                rev = self._design_rev
            return {"ok": True, "design_rev": rev, "design": d}
        if cmd == "list_files":
            return self._list_files(req)
        if cmd == "get_file":
            return self._get_file(req)
        if cmd == "get_layouts":
            # the Control tab layouts saved on this PC, so a watcher offers the
            # lab's panels ("CamKimP1") instead of its own, different list
            try:
                # the file apps/control_panel.py keeps them in (LAYOUTS_PATH),
                # named here so the server need not import Qt for a path
                path = Path(__file__).resolve().parent.parent / "suite_layouts.json"
                layouts = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                layouts = {}
            return {"ok": True, "layouts": layouts if isinstance(layouts, dict) else {}}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    # ---- what was submitted, and how the suite on this PC shows it ------
    def _get_scan(self) -> dict:
        """The submitted queue as it is: every entry's definition, run info and
        result, which one runs, who started it. A watcher shows this read-only
        (and can copy a definition into its own Scan tab)."""
        with self._lock:
            entries = [{"name": e.name,
                        "recipe": json.loads(e.recipe.to_json()),
                        "attrs": dict(e.attrs),
                        "n_points": int(e.n_points),
                        "result": e.result, "path": e.path, "error": e.error}
                       for e in self._entries]
            return {"ok": True, "scan_rev": self._scan_rev, "entries": entries,
                    "current": self._qi if self._busy else -1, "busy": self._busy,
                    "started_by": self._started_by}

    # ---- the saved measurements, for a watcher's Data tab -----------------
    #
    # Lukas 2026-10-06, watching from the office: "would there be a
    # possibility to view the actually measured file in the data viewer?" The
    # files stay on this PC; a watcher lists them and fetches a COPY of one.
    # Only .nc files inside the data folder, never a path a client chose.

    def _index_now(self, ddir: Path) -> None:
        if self._indexer is not None and self._indexer.is_alive():
            return

        def work():
            from . import catalogue
            try:
                catalogue.scan(ddir)
            except Exception as exc:
                self.log(f"indexing the data folder failed: {exc}", "warn")
        self._indexer = threading.Thread(target=work, name="scanserver-index", daemon=True)
        self._indexer.start()

    def _list_files(self, req) -> dict:
        """What the catalogue knows of the data folder, newest first, paths
        RELATIVE to it. Starts a re-index in the background; `indexing` says
        whether one is running (the list may grow on the next call)."""
        from . import catalogue
        ddir = self._data_dir()
        self._index_now(ddir)
        try:
            limit = max(1, min(int(req.get("limit") or 500), 5000))
            rows = catalogue.search(ddir, text=req.get("text") or None, limit=limit)
        except Exception as exc:
            rows, err = [], str(exc)
        else:
            err = ""
        out = []
        for r in rows:
            try:
                rel = Path(r["path"]).resolve().relative_to(ddir.resolve())
            except (KeyError, ValueError, OSError):
                continue                       # not under the data folder: never offered
            try:
                size = (ddir / rel).stat().st_size
            except OSError:
                continue
            out.append({"path": rel.as_posix(), "name": r.get("name") or rel.stem,
                        # the catalogue keeps each axis as {name, size, ...}: say
                        # it as "field (41)", outer first
                        "measured": r.get("created") or r.get("measured") or "",
                        "dims": [f"{d.get('name', '?')} ({d.get('size', '?')})"
                                 if isinstance(d, dict) else str(d)
                                 for d in (r.get("dims") or [])],
                        "detectors": r.get("detectors") or [],
                        "n_points": r.get("n_points"), "sample": r.get("sample") or "",
                        "operator": r.get("operator") or "", "bytes": size})
        return {"ok": True, "files": out, "data_dir": str(ddir), "pc": self.pc,
                "indexing": bool(self._indexer and self._indexer.is_alive()),
                **({"warning": err} if err else {})}

    def _get_file(self, req) -> dict:
        """One chunk of a saved measurement: {data (base64), offset, total}."""
        import base64
        ddir = self._data_dir().resolve()
        rel = str(req.get("path") or "")
        try:
            path = (ddir / rel).resolve()
            path.relative_to(ddir)              # inside the data folder, or refused
        except (ValueError, OSError):
            return {"ok": False, "error": "not a file of this server's data folder"}
        if path.suffix.lower() != ".nc" or path.name.endswith(".writing.nc") \
                or not path.is_file():
            return {"ok": False, "error": f"no measurement {rel!r} here"}
        offset = max(0, int(req.get("offset") or 0))
        size = max(1, min(int(req.get("size") or FILE_CHUNK), FILE_CHUNK))
        total = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(size)
        return {"ok": True, "offset": offset, "total": total,
                "mtime": path.stat().st_mtime,
                "data": base64.b64encode(chunk).decode("ascii")}

    def _set_design(self, req) -> dict:
        """The Navigator's design file, from the suite on THIS PC (base64 in
        `data`), so a watcher can draw it. At most DESIGN_MAX_BYTES."""
        ident = ControlLease._identity(req)
        if ident is None or pc_of(ident) != self.pc:
            return {"ok": False, "refused": "not_this_pc",
                    "error": "the design is shared by the measurement suite on the "
                             "scan server's own PC"}
        data = req.get("data")
        if not isinstance(data, str) or len(data) > DESIGN_MAX_BYTES * 4 // 3 + 8:
            return {"ok": False, "error": f"no design data, or larger than "
                                          f"{DESIGN_MAX_BYTES // 2**20} MB"}
        design = {"name": str(req.get("name") or "design"),
                  "kind": str(req.get("kind") or "gds"),
                  "cell": str(req.get("cell") or ""),
                  "width_um": float(req.get("width_um") or 0.0),
                  "data": data}
        with self._lock:
            if self._design != design:
                self._design = design
                self._design_rev += 1
            rev = self._design_rev
        return {"ok": True, "design_rev": rev}

    def _set_view(self, req) -> dict:
        """The plot choice of the measurement suite on THIS PC (detector, X/Y,
        held slices, colour range). Only this PC's suite sets it -- a watcher
        follows it; a second PC must not steer what the lab sees."""
        ident = ControlLease._identity(req)
        if ident is None or pc_of(ident) != self.pc:
            return {"ok": False, "refused": "not_this_pc",
                    "error": "the shown view is set by the measurement suite on the "
                             "scan server's own PC"}
        view = req.get("view")
        if not isinstance(view, dict):
            return {"ok": False, "error": "'view' must be a JSON object"}
        try:
            if len(json.dumps(view)) > 20000:
                return {"ok": False, "error": "view too large"}
        except (TypeError, ValueError):
            return {"ok": False, "error": "view must be plain JSON"}
        with self._lock:
            if view != self._view:
                self._view = view
                self._view_rev += 1
                self._view_by = describe_holder(ident)
            rev = self._view_rev
        return {"ok": True, "view_rev": rev}

    # ---- config --------------------------------------------------------
    def _config(self) -> dict:
        # data_dir is SHOWN, never set over the wire: the folder belongs to the
        # PC the server runs on (its Settings tab), and a client elsewhere must
        # not choose where this process writes (the modules' rule too)
        return {"server": {"data_dir": str(self._data_dir()),
                           "live_every_s": self.live_every_s,
                           "follow": self.follow}}

    def _set_config(self, cfg: dict) -> dict:
        srv = cfg.get("server") or {}
        if "live_every_s" in srv:
            self.live_every_s = max(0.2, float(srv["live_every_s"]))
        return {"ok": True, "config": self._config()}

    # ---- live data -----------------------------------------------------
    def _set_live(self, ds) -> None:
        with self._lock:
            self._live_ds = ds
            self._live_rev += 1
            self._live_blob = None
            rev = self._live_rev
        self._out.put((TOPIC_LIVE, {"live_rev": rev}))

    def _get_live(self, have_rev) -> dict:
        with self._lock:
            ds, rev, blob = self._live_ds, self._live_rev, self._live_blob
        try:
            have = int(have_rev) if have_rev is not None else -1
        except (TypeError, ValueError):
            have = -1
        if ds is None:
            return {"ok": True, "live_rev": rev, "data": None}
        if rev <= have:
            return {"ok": True, "live_rev": rev, "unchanged": True}
        if blob is None or blob[0] != rev:
            text = dataset_to_text(ds)
            with self._lock:
                if self._live_rev == rev:
                    self._live_blob = (rev, text)
        else:
            text = blob[1]
        return {"ok": True, "live_rev": rev, "encoding": LIVE_ENCODING, "data": text}

    # ---- submit --------------------------------------------------------
    def _same_pc(self, req) -> str:
        """'' when the request comes from THIS PC, else why not (phase 1)."""
        ident = ControlLease._identity(req)
        if ident is None:
            return ("submit needs a client identity (\"client\": {\"id\", \"kind\", "
                    "\"name\", \"host\"}) -- a scan is accepted only from this PC")
        pc = pc_of(ident)
        if pc != self.pc:
            return (f"a scan can only be started from the scan server's own PC "
                    f"('{self.pc}') in phase 1; this request comes from "
                    f"{describe_holder(ident)}. Watching, Abort and Stop queue work "
                    f"from every PC; starting scans from another PC is phase 2")
        return ""

    def _submit(self, req, raw_entries, common_attrs, allow_unsaved) -> dict:
        why = self._same_pc(req)
        if why:
            return {"ok": False, "refused": "phase2", "error": why}
        if not isinstance(raw_entries, list) or not raw_entries:
            return {"ok": False, "error": "nothing to run (no recipe / an empty queue)"}
        with self._lock:
            if self._busy:
                name = self._entries[self._qi].name if 0 <= self._qi < len(self._entries) else ""
                return {"ok": False, "refused": "busy",
                        "error": f"a scan is already running on this server ('{name}'); "
                                 f"wait for it to end or abort it (adding to a running "
                                 f"queue is phase 2)"}
        entries = []
        for k, item in enumerate(raw_entries):
            if not isinstance(item, dict) or not isinstance(item.get("recipe"), dict):
                return {"ok": False, "error": f"entry {k + 1}: 'recipe' must be a scan "
                                              f"definition (a JSON object)"}
            try:
                recipe = Recipe.from_dict(item["recipe"])
            except Exception as exc:
                return {"ok": False, "error": f"entry {k + 1}: not a scan definition ({exc})"}
            name = str(item.get("name") or recipe.name or "scan").strip() or "scan"
            attrs = {**_clean_attrs(common_attrs), **_clean_attrs(item.get("attrs"))}
            entries.append(_Entry(name, recipe, attrs))

        # every entry checked BEFORE anything runs: the third scan must not turn
        # out to be invalid at two in the morning (scan_queue.validate_queue)
        problems = self._validate(entries)
        if problems and self.follow:
            # a module started a moment ago: look once more, then judge
            self.follow_once(force=True)
            problems = self._validate(entries)
        if problems:
            return {"ok": False, "refused": "invalid",
                    "error": "not started -- " + " | ".join(problems)}
        ddir = self._data_dir()
        ok, msg = autosave.probe_save_target(ddir)
        if not ok and not allow_unsaved:
            return {"ok": False, "refused": "unsaved",
                    "error": f"{msg} -- fix the data folder on the scan server's PC "
                             f"(its measurement suite, Settings tab), or submit with "
                             f"allow_unsaved"}
        ident = ControlLease._identity(req) or {}
        with self._lock:
            if self._busy:
                return {"ok": False, "refused": "busy", "error": "a scan started meanwhile"}
            self._busy = True
            self._entries = entries
            self._scan_rev += 1
            self._qi = -1
            self._stop_reason = ""
            self._abort = False
            self._last_error = ""
            self._last_summary = ""
            self._started_by = describe_holder(ident) if ident else ""
            self._t_queue = time.monotonic()
            self._reset_scan_state()
            self._runner = threading.Thread(target=self._run_queue, args=(ddir if ok else None,),
                                            name="scanserver-run", daemon=True)
            self._runner.start()
        what = (f"scan '{entries[0].name}'" if len(entries) == 1
                else f"queue of {len(entries)} scans")
        self.log(f"{what} submitted by {self._started_by or 'a client'}")
        return {"ok": True, "accepted": True, "n": len(entries),
                "names": [e.name for e in entries],
                "data_dir": str(ddir) if ok else "", "save_ok": ok, "save_note": msg}

    def _validate(self, entries) -> list[str]:
        out = []
        with self._lock:
            reg = self.registry
        for e in entries:
            try:
                errs = list(e.recipe.validate(reg))
            except Exception as exc:
                errs = [str(exc)]
            if not e.recipe.axes:
                errs.append("no axes")
            if not errs:
                try:
                    e.n_points = int(e.recipe.compile(reg).n_points)
                except Exception:
                    e.n_points = 0
            if errs:
                out.append(f"{e.name}: {'; '.join(errs)}")
        return out

    # ---- abort / stop / answer / clear -----------------------------------
    def _who(self, req) -> str:
        ident = ControlLease._identity(req)
        return describe_holder(ident) if ident else "a client"

    def _abort_verb(self, req) -> dict:
        with self._lock:
            if not self._busy:
                return {"ok": True, "running": False}
            self._abort = True
            name = self._entries[self._qi].name if 0 <= self._qi < len(self._entries) else ""
        self.log(f"ABORT pressed by {self._who(req)} ('{name}')", "warn")
        return {"ok": True, "running": True, "aborting": name}

    def _abort_all(self, reason: str) -> bool:
        with self._lock:
            if not self._busy:
                return False
            self._stop_reason = self._stop_reason or reason
            self._abort = True
        return True

    def _stop_queue_verb(self, req) -> dict:
        who = self._who(req)
        if not self._abort_all(f"stopped by {who}"):
            return {"ok": True, "running": False}
        self.log(f"STOP QUEUE pressed by {who}", "warn")
        return {"ok": True, "running": True}

    def _answer(self, answer, req) -> dict:
        if isinstance(answer, str):
            a = answer.strip().lower()
            answer = ("all" if a == "all" else
                      True if a in ("true", "continue", "yes", "1") else False)
        elif answer != "all":
            answer = bool(answer)
        with self._lock:
            ask, self._ask = self._ask, None
        if ask is None:
            return {"ok": False, "error": "no question is open (the scan is not "
                                          "waiting for the operator)"}
        word = ("Abort ALL" if answer == "all" else "Continue" if answer else "Abort scan")
        self.log(f"operator: {word} (at the pause) -- {self._who(req)}")
        try:
            ask[1](answer)
        except Exception as exc:
            return {"ok": False, "error": f"the answer did not reach the scan: {exc}"}
        return {"ok": True, "answer": answer}

    def _clear_fault(self, module: str, req) -> dict:
        lab = self.lab
        if lab is None:
            return {"ok": False, "error": "no instrument is connected"}
        try:
            lab.clear_fault(module)
        except Exception as exc:
            self.log(f"clear_fault on {module} refused: {exc}", "warn")
            return {"ok": False, "error": f"clear_fault on {module} refused: {exc}"}
        self.log(f"clear_fault sent to {module} ({self._who(req)})")
        return {"ok": True}

    def _shutdown(self, force: bool) -> dict:
        """The launcher's clean stop. While a scan runs: Abort it and the
        queue, let the after-scan routine run and the data be saved, THEN exit
        (see the module docstring for why not a refusal)."""
        with self._lock:
            busy = self._busy
            name = self._entries[self._qi].name if (busy and 0 <= self._qi < len(self._entries)) else ""
            self._shutting_down = True
        if not busy:
            self._stop.set()
            return {"ok": True, "stopping": True}
        self.log(f"shutdown asked while '{name}' runs: aborting it (data is kept), "
                 f"then stopping", "warn")
        self._abort_all("the scan server was shut down")

        def wait_then_stop():
            r = self._runner
            if r is not None:
                r.join(timeout=60)
            self._stop.set()
        threading.Thread(target=wait_then_stop, daemon=True).start()
        return {"ok": True, "stopping": True, "aborting": name}

    # ------------------------------------------------------------------ #
    # the runner: one queue, one scan after another
    # ------------------------------------------------------------------ #
    def _run_queue(self, data_dir) -> None:
        entries = self._entries
        try:
            if len(entries) > 1:
                self.log(f"queue: {len(entries)} scans")
            for i, e in enumerate(entries):
                with self._lock:
                    if self._stop_reason:
                        break
                    self._qi = i
                    self._abort = False
                    self._reset_scan_state()
                if len(entries) > 1:
                    self.log(f"queue: scan {i + 1} of {len(entries)} '{e.name}' started")
                self._run_one(e, data_dir)
                if e.result == "error":
                    # a module that died fails the next scan the same way
                    with self._lock:
                        self._stop_reason = self._stop_reason or f"'{e.name}' failed: {e.error}"
                    if len(entries) > 1:
                        self.log(f"queue: scan {i + 1} '{e.name}' FAILED ({e.error}); "
                                 f"queue stopped", "error")
                    break
                if e.stop_all:
                    with self._lock:
                        self._stop_reason = self._stop_reason or f"abort all: {e.stop_reason}"
                    if len(entries) > 1:
                        self.log(f"queue: scan {i + 1} '{e.name}' {e.result} -- ABORT ALL, "
                                 f"queue stopped", "warn")
                    break
                if len(entries) > 1:
                    self.log(f"queue: scan {i + 1} '{e.name}' {e.result}")
        except Exception as exc:              # never leave the server "busy"
            self._last_error = f"{type(exc).__name__}: {exc}"
            self.log(f"the scan runner failed: {self._last_error}", "error")
        finally:
            counts: dict = {}
            for e in entries:
                if e.result:
                    counts[e.result] = counts.get(e.result, 0) + 1
            parts = [f"{counts[k]} {k}" for k in ("done", "aborted", "error") if counts.get(k)]
            not_run = sum(1 for e in entries if e.result is None)
            if not_run:
                parts.append(f"{not_run} not run")
            text = ("Queue finished: " if len(entries) > 1 else "Scan finished: ") + \
                ", ".join(parts or ["nothing ran"])
            if self._stop_reason:
                text += f" - {self._stop_reason}"
            with self._lock:
                self._last_summary = text
                self._busy = False
                self._ask = None
                self._faults = []
                lab = self.lab
            if lab is not None:
                lab.set_abort(None)
            self.log(text)

    def _run_one(self, e: _Entry, data_dir) -> None:
        """One scan, exactly as the suite's ScanWorker runs it."""
        recipe = e.named_recipe()
        path = autosave.autosave_path(data_dir, recipe.name) if data_dir else None
        with self._lock:
            reg, lab = self.registry, self.lab
            self._save_path = str(path) if path else ""
            self._t_scan = time.monotonic()
        e.path = str(path) if path else ""
        if lab is not None:
            # an Abort must reach an instrument that is mid-settle too
            lab.set_abort(lambda: self._abort)
        self.log(f"scan '{e.name}' started" +
                 (f", saving to {path}" if path else " -- NOT saved (no writable data folder)"))
        try:
            ds = run(recipe, reg,
                     on_progress=self._progress,
                     should_abort=lambda: self._abort,
                     on_point=self._on_point,
                     on_log=self.log,
                     created_iso=datetime.now().isoformat(timespec="seconds"),
                     data_path=path,
                     on_fault=self._on_fault,
                     attrs=e.attrs,
                     on_pause=self._on_pause)
            self._final(ds, path)
            e.result = "aborted" if (self._abort or ds.attrs.get("stopped_by")) else "done"
            e.stop_all = ds.attrs.get("stopped_scope") == "all"
            e.stop_reason = str(ds.attrs.get("stopped_by", ""))
            self.log(f"scan '{e.name}' {e.result}")
        except RoutineError as exc:
            if exc.dataset is not None:
                self._final(exc.dataset, path)
            e.result, e.error = "error", str(exc)
            self._last_error = str(exc)
            self.log(f"scan '{e.name}' FAILED: {exc}", "error")
        except ScanAborted as exc:
            e.result = "aborted"
            e.stop_all = bool(getattr(exc, "whole_queue", False))
            e.stop_reason = str(getattr(exc, "reason", "") or "")
            if getattr(exc, "dataset", None) is not None:
                self._final(exc.dataset, path)
            self.log(f"scan '{e.name}' aborted ({exc})", "warn")
        except ScanFault as exc:
            if getattr(exc, "dataset", None) is not None:
                self._final(exc.dataset, path)
            e.result, e.error = "error", str(exc)
            self._last_error = str(exc)
            self.log(f"scan '{e.name}' FAILED: {exc}", "error")
        except Exception as exc:
            # the engine hands over the points measured before the error
            if getattr(exc, "dataset", None) is not None:
                self._final(exc.dataset, path)
            e.result, e.error = "error", f"{exc}"
            self._last_error = str(exc)
            self.log(f"scan '{e.name}' FAILED: {exc}", "error")
        finally:
            with self._lock:
                self._ask = None
                self._faults = []

    # ---- engine callbacks (the scan thread) ------------------------------
    def _progress(self, done, total, eta, where=None):
        with self._lock:
            self._done, self._total, self._eta = int(done), int(total), float(eta)
            if where is not None:
                self._where = where

    def _on_point(self, done, total, snapshot):
        """Live snapshot every live_every_s, checkpoints every 1/10 of a long
        scan -- the suite's ScanWorker._live, word for word in effect."""
        if not self._ck_every:
            self._ck_every = max(1, total // 10) if total > CHECKPOINT_ABOVE else 0
            self._ck_next = self._ck_every
        now = time.monotonic()
        due_save = bool(self._ck_every and done < total and done >= self._ck_next)
        if due_save:
            self._ck_next = (done // self._ck_every + 1) * self._ck_every
        due_live = done >= total or now - self._last_live >= self.live_every_s
        if not (due_save or due_live):
            return
        ds = snapshot()
        if due_live:
            self._last_live = now
            self._set_live(ds)
        if due_save:
            self._write(ds, self._save_path or None, done, total)

    def _write(self, ds, path, done, total) -> None:
        if not path:
            return
        try:
            autosave.write_dataset(ds, path)
            with self._lock:
                self._last_saved = str(path)
                self._save_error = ""
            if done < total:
                self.log(f"saved {done}/{total} points to {path}")
        except Exception as exc:              # a full disk must not kill the scan
            msg = f"could not save to {path}: {exc}"
            with self._lock:
                self._save_error = msg
            self.log(msg, "error")

    def _final(self, ds, path) -> None:
        """The scan's last dataset: shown (live) and saved for good."""
        self._set_live(ds)
        n = int(np.prod([ds.sizes[d] for d in ds.sizes])) if ds.sizes else 0
        self._write(ds, str(path) if path else None, n, n)
        if path:
            self.log(f"saved to {path}")

    def _on_fault(self, faults) -> None:
        with self._lock:
            self._faults = list(faults or [])
        if faults:
            self.log("PAUSED: " + "; ".join(f"{f[0]}: {f[1]}" for f in faults), "warn")

    def _on_pause(self, message, answer) -> None:
        with self._lock:
            self._ask = None if answer is None else (message or "", answer)
        if answer is not None:
            self.log(f"WAITING FOR THE OPERATOR: {message}", "warn")


def _json(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def main(argv=None) -> int:
    """Command line of scripts/run_scan_server.py (the launcher's contract:
    --cmd-port, --pub-port, --real)."""
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="AaltoFlow scan server: run scans as a "
                                             "service, watch them from any PC")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="accepted for the launcher's contract and ignored: the scan "
                         "server drives whatever modules run on this PC, real or simulated")
    ap.add_argument("--sim", action="store_true",
                    help="use scan-core's simulated registry instead of the running modules")
    ap.add_argument("--data-dir", default=None,
                    help="data folder (default: this PC's suite setting, else scan-core/out)")
    args = ap.parse_args(argv)

    reg = None
    if args.sim:
        from .registry import build_sim_registry
        reg = build_sim_registry()
    srv = ScanServer(host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port,
                     registry=reg, data_dir=args.data_dir)
    print(f"scan server on tcp://{args.host}:{args.cmd_port} (cmd) / {args.pub_port} (pub)"
          + (" -- SIMULATED registry" if args.sim else " -- following the launcher"),
          flush=True)
    print("Ctrl-C to stop.", flush=True)
    try:
        srv.serve_forever()
    except PortInUse as exc:
        print(f"scan server: cannot start: {exc}", file=sys.stderr)
        return 2
    except secure.SecurityError as exc:
        print(f"scan server: cannot start: {exc}", file=sys.stderr)
        return 3
    return 0
