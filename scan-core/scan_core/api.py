"""api.py -- drive the lab from a Python script.

The measurement suite is the GUI way to run scans. This is the SCRIPT way, for
everything a fixed recipe cannot say: "cool to 5 K, wait until the temperature
has been stable for ten minutes, focus, then map; repeat at 10, 20 and 50 K".

    from scan_core import api

    with api.connect() as lab:              # the services Mission Control runs
        lab.set("clMag.field", 50)          # blocks until the field has settled
        lab.wait_until("ppms.temperature_stable", hold_s=600, timeout_s=7200)
        lab.run("camera.autofocus")         # waits until done; raises if it failed
        for t in [5, 10, 20, 50]:
            lab.set("ppms.temperature", t)
            ds = lab.scan("recipes/field_map.yaml", name=f"map_{t}K")
        v = lab.get("hf2.r1")               # one fresh reading

Nothing in here is a second copy of the suite. A script uses the SAME pieces
the GUI uses: the registry built from every module's `describe` (lab.py), the
blocking `set` with the module's own settle rule (registry.py), the engine that
runs a recipe (engine.py), and the GUI's file naming (autosave.py). So a recipe
saved in the Scan Builder runs here unchanged, and a file written here looks
exactly like one written by the suite.

Control and safety -- the decisions, written down (docs/SCRIPTING.md has the
long version):

* A script is treated EXACTLY LIKE A SCAN. It identifies itself to every
  instrument the way the scan engine does ("machine" with the scan role), and
  before the first command that changes an instrument it CLAIMS that
  instrument (`claim_scan`, suite_common/control.py). The claim is the
  service's own rule, so:
    - if a person holds control from ANOTHER PC, the claim is refused and the
      call raises ControlRefused -- nothing was sent;
    - if another scan (a suite, another script) is using the instrument, the
      same: ControlRefused;
    - if nobody holds control, this PC gets it for as long as the script runs
      (a GUI elsewhere becomes a viewer meanwhile), exactly as for a scan;
    - a GUI on THIS PC does not block the script (control belongs to a PC).
  A script therefore never slips past the control lock the way a bare
  "machine" client would.
* The claim is kept until the `with` block ends (or `lab.release()`): a script
  is one long experiment, and another scan must not start driving the magnet
  between two of its steps. Heartbeats keep it alive through hours of waiting;
  a script that crashes frees its instruments after 10 s like any client.
* Reading never claims anything (`read`, `wait_until`, `parameters`,
  `describe`). `get` of a SLOW detector does, because it triggers an
  acquisition on the instrument.
* Ctrl+C during `lab.scan(...)` ABORTS THE SCAN CLEANLY: the scan stops after
  the point (or settle wait) it is in, the after-scan routine runs, the points
  measured so far are SAVED, and then KeyboardInterrupt ends the script. Press
  Ctrl+C a second time to stop at once (what was measured is still saved).
* Values outside a parameter's limits are REFUSED, not clamped: in a script a
  typo (500 instead of 50) must stop the script, not drive the magnet to its
  limit.

Simulation: `api.connect(simulate=True)` gives the toy instruments of
`build_sim_registry()` (ids without a module prefix: "field", "lockin_r", ...),
so every example and test runs without the lab.
"""

from __future__ import annotations

import copy
import difflib
import json
import math
import operator
import re
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from . import autosave
from .engine import run as _engine_run
from .errors import RoutineError, ScanAborted, ScanFault, ScanStopped
from .recipe import Recipe


# ───────────────────────────────── errors ─────────────────────────────────────
#
# One family, so a script can `except api.ScriptError` and catch everything
# this module raises on purpose. Each message is written for the person at the
# bench: what was asked, what happened, and what to do about it.

class ScriptError(RuntimeError):
    """Base class of every error this API raises on purpose."""


class ParameterNotFound(ScriptError, LookupError):
    """No parameter / detector / action with that id is connected."""


class SettleTimeout(ScriptError, TimeoutError):
    """A set (or an acquisition) did not settle within its timeout."""


class WaitTimeout(ScriptError, TimeoutError):
    """`wait_until` gave up: the condition did not hold (long enough) in time."""


class ActionFailed(ScriptError):
    """An action (autofocus, take reference ...) failed or did not finish."""


class ControlRefused(ScriptError):
    """The instrument refused: someone on another PC has control of it, or
    another scan is using it. Nothing was sent."""


class RecipeInvalid(ScriptError, ValueError):
    """The recipe names unknown parameters or sweeps outside the limits."""


class CannotSave(ScriptError, OSError):
    """The data cannot be written where it should go."""

    dataset = None


class ScanStoppedAll(ScriptError):
    """A routine step chose "Abort all" (abort_if with scope all, a wait_until
    with on_timeout stop_all, Abort all at a pause): the scan stopped, its
    measured points are saved (`.dataset`, `.path`), and a script running
    several scans should stop too -- catch it only to clean up. "Abort scan"
    does NOT raise: scan() returns the partial dataset, the reason in its
    attribute `stopped_by` (lab PC, 2026-10-05: both used to arrive as a
    KeyboardInterrupt "aborted by Ctrl+C", which was wrong twice)."""


class NoInstruments(ScriptError):
    """`connect()` found no running service to connect to."""


#: attributes the engine writes into every file; **meta must not replace them
_RESERVED_ATTRS = {"name", "comment", "recipe_json", "created", "n_points",
                   "seconds", "dims", "window_json", "window_detector"}


def path_of(ds) -> Path | None:
    """The file a dataset from `Lab.scan` was saved to (None if not saved).

    xarray datasets cannot carry new attributes, so the path is kept where
    xarray itself keeps "the file this came from": `ds.encoding["source"]`.
    """
    src = getattr(ds, "encoding", {}).get("source")
    return Path(src) if src else None


# ─────────────────────────────── connecting ───────────────────────────────────

def connect(endpoints: dict | None = None, host: str | None = None,
            include=None, simulate: bool = False, data_dir=None, log=print,
            timeout_ms: int = 3000, root=None, name: str | None = None) -> "Lab":
    """Connect to the lab and return a `Lab` (use it in a `with` block).

    Which instruments, in order of precedence:

    * `simulate=True`: the simulated instruments (no hardware, no services).
    * `endpoints={"clMag": ("localhost", 5555, 5556), ...}`: exactly these
      services (the name becomes the parameter prefix: "clMag.field").
    * otherwise the modules Mission Control knows about (module discovery,
      this PC's suite_local.json) -- like the suite's "follow the launcher":
      every one that answers is connected. `include=["clMag", "hf2"]` limits
      it to those (and then they MUST answer). `host="lab-pc"` looks for the
      local modules on another PC (remote services keep their own host).

    `data_dir`: where `scan()` saves (default: the folder chosen in the
    suite's Settings tab, else scan-core/out). `log`: where messages go
    (default print; None = silent). `name`: how the script appears on the
    instruments' control bars (default: the script's file name).
    """
    logger = _Logger(log)
    script = name or _script_name()
    ddir = Path(data_dir) if data_dir else autosave.default_data_dir(root)

    if simulate:
        from .registry import build_sim_registry
        logger("connected to the SIMULATED instruments (no hardware)")
        return Lab(build_sim_registry(), None, data_dir=ddir, log=logger,
                   name=script)

    from .lab import build_lab_registry      # imports pyzmq: only when needed
    if endpoints is None:
        endpoints = _discover_endpoints(host, include, root, logger)
    else:
        endpoints = {k: _endpoint(v) for k, v in endpoints.items()
                     if not include or k in include}
    if not endpoints:
        raise NoInstruments(
            "no instrument service is running (none answered). Start the "
            "services in Mission Control first -- or use "
            "api.connect(simulate=True) to try a script without the lab.")
    logger(f"connecting to {', '.join(endpoints)} ...")
    reg, conn = build_lab_registry(include=tuple(endpoints), endpoints=endpoints,
                                   prefix=True, timeout_ms=timeout_ms,
                                   on_warn=lambda m: logger(f"note: {m}"))
    lab = Lab(reg, conn, data_dir=ddir, log=logger, name=script)
    logger(f"connected: {len(reg.settables())} settable parameters, "
           f"{len(reg.gettables())} detectors, {len(reg.actions())} actions")
    return lab


def _endpoint(v) -> tuple:
    """(host, cmd) or (host, cmd, pub) -> (host, cmd, pub)."""
    v = tuple(v)
    if len(v) == 2:
        return (v[0], int(v[1]), int(v[1]) + 1)
    return (v[0], int(v[1]), int(v[2]))


def _discover_endpoints(host, include, root, logger) -> dict:
    """{slug: (host, cmd, pub)} of the modules Mission Control knows about.

    Without `include`: every module that answers (probed in parallel; a
    switched-off PC must not cost a timeout per module, gotcha #21). With it:
    exactly those, whether they answer or not -- the connect then fails
    loudly, naming the one that is missing.
    """
    from concurrent.futures import ThreadPoolExecutor

    from suite_common import discover, probe

    found = discover(root)

    def where(m):
        return host if (host and not m.remote) else m.host

    if include:
        out = {}
        for n in include:
            m = next((m for m in found.modules if n in (m.id, m.slug, m.key)), None)
            if m is None:
                known = ", ".join(sorted(m.slug for m in found.modules))
                raise ParameterNotFound(f"no module called {n!r} in Mission "
                                        f"Control's list (known: {known})")
            out[m.slug] = (where(m), m.cmd, m.pub)
        return out
    mods = list(found.modules)
    if not mods:
        return {}
    with ThreadPoolExecutor(max_workers=min(16, len(mods))) as pool:
        ups = list(pool.map(lambda m: probe(where(m), m.cmd, 0.3), mods))
    return {m.slug: (where(m), m.cmd, m.pub) for m, up in zip(mods, ups) if up}


def _script_name() -> str:
    try:
        stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    except Exception:
        stem = ""
    return f"script {stem}" if stem and stem not in ("-c", "") else "script"


class _Logger:
    """Time-stamped messages to `log` (print by default); kept in `lines`."""

    def __init__(self, sink):
        self.sink = sink
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        self.lines.append(line)
        if self.sink is not None:
            try:
                self.sink(line)
            except Exception:
                pass                    # a broken log must never stop a scan


# ─────────────────────────────── conditions ───────────────────────────────────
#
# TODO(routine-steps): when scan_core/expr.py (the restricted evaluator of the
# routine-steps branch) is on main, parse string conditions with it instead,
# so a script and a routine accept exactly the same expressions. Until then:
# "<pid> <op> <number>", "<pid>" (true when the value is) and "not <pid>".

_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt,
        ">=": operator.ge, "==": operator.eq, "!=": operator.ne}
_COND = re.compile(
    r"^\s*(?P<neg>not\s+)?(?P<pid>[A-Za-z_][\w.\-]*)\s*"
    r"(?:(?P<op><=|>=|==|!=|<|>)\s*"
    r"(?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?))?\s*$")


def parse_condition(text: str):
    """"ppms.temperature < 5.1" -> (pid, test(value) -> bool). Raises ValueError."""
    m = _COND.match(text or "")
    if not m or (m.group("neg") and m.group("op")):
        raise ValueError(
            f"cannot read the condition {text!r}. Write '<parameter> <op> "
            f"<number>' (op one of < <= > >= == !=), '<parameter>' or "
            f"'not <parameter>' -- or pass a function: lambda: ...")
    pid, op, neg = m.group("pid"), m.group("op"), bool(m.group("neg"))
    if op:
        num, fn = float(m.group("num")), _OPS[op]

        def test(v):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return False
            return not math.isnan(v) and fn(v, num)
    else:
        def test(v):
            if isinstance(v, float) and math.isnan(v):
                truth = False
            else:
                truth = bool(v)
            return (not truth) if neg else truth
    return pid, test


# ─────────────────────────────────── Lab ──────────────────────────────────────

class Lab:
    """A connected lab: set, get, run actions, wait, scan. See the module doc.

    Made by `connect()`; use it in a `with` block so the instruments are
    given back (claims released, heartbeats stopped, sockets closed) however
    the script ends.
    """

    def __init__(self, registry, connections=None, data_dir=None, log=None,
                 name: str = "script"):
        self.registry = registry
        #: the scan_core.lab.Lab of instrument connections (None when simulated)
        self.connections = connections
        self.data_dir = Path(data_dir) if data_dir else autosave.default_data_dir()
        self._log = log if isinstance(log, _Logger) else _Logger(log)
        self.name = name
        #: the file the last `scan()` was saved to
        self.last_path: Path | None = None
        self._held: set[str] = set()          # instruments this script has claimed
        self._unprotected: set[str] = set()   # ... that could not be claimed (old service)
        self._aborting = False
        self._closed = False
        if connections is not None:
            # every settle wait sees a Ctrl+C during a scan (see scan())
            connections.set_abort(lambda: self._aborting)
            # A scan run from a script claims through the SCRIPT, so the
            # instruments stay claimed between scans (see _scan_claim).
            registry.scan_claim = self._scan_claim

    # ---- context manager --------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is KeyboardInterrupt:
            self._log("stopped by Ctrl+C")
        self.close()
        return False

    def close(self) -> None:
        """Give every instrument back and disconnect. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self.release()
        if self.connections is not None:
            self.connections.set_abort(None)
            self.connections.close()

    def release(self) -> None:
        """Give back the instruments this script has claimed (before the end).

        The next command that changes one claims it again."""
        if self.connections is None:
            return
        for n in sorted(self._held):
            inst = self.connections.instruments.get(n)
            if inst is not None:
                inst.release_scan()
        if self._held:
            self._log(f"released {', '.join(sorted(self._held))}")
        self._held.clear()

    # ---- messages ---------------------------------------------------------

    def log(self, text: str) -> None:
        """Print a time-stamped line (and keep it in `lab.log_lines`)."""
        self._log(str(text))

    @property
    def log_lines(self) -> list[str]:
        return list(self._log.lines)

    # ---- discovery --------------------------------------------------------

    def parameters(self, kind: str | None = None) -> list[str]:
        """Every id you can use, sorted. `kind`: "settable", "detector" or "action"."""
        reg = self.registry
        groups = {"settable": [p.id for p in reg.settables()],
                  "detector": [p.id for p in reg.gettables()],
                  "action": [a.id for a in reg.actions()]}
        if kind is not None:
            if kind not in groups:
                raise ValueError(f"kind must be one of {sorted(groups)}, not {kind!r}")
            return sorted(groups[kind])
        return sorted(groups["settable"] + groups["detector"] + groups["action"])

    def describe(self, pid: str) -> dict:
        """What one id is: kind, label, unit, limits, slow detector or not ..."""
        reg = self.registry
        owner = (getattr(reg, "owner", None) or {}).get(pid)
        a = reg.get_action(pid)
        if a is not None:
            return {"id": pid, "kind": "action", "label": a.label, "help": a.help,
                    "takes_args": bool(getattr(a, "takes_args", False)),
                    "instrument": owner}
        p = self._param(pid)
        d = {"id": pid, "label": p.label, "unit": p.unit, "instrument": owner}
        if p.kind == "settable":
            d.update(kind="settable", limits=tuple(float(x) for x in p.limits),
                     integer=bool(getattr(p, "integer", False)))
        else:
            d.update(kind="detector", dtype=getattr(p, "dtype", "float"),
                     slow=getattr(p, "acquire", None) is not None,
                     axes=[ax.name for ax in getattr(p, "axes", ()) or ()])
        return d

    def summary(self) -> str:
        """A readable table of everything connected (print(lab.summary()))."""
        rows = []
        for pid in self.parameters():
            d = self.describe(pid)
            if d["kind"] == "settable":
                lo, hi = d["limits"]
                extra = f"{lo:g} .. {hi:g} {d['unit']}".strip()
            elif d["kind"] == "detector":
                extra = (d["unit"] or "") + ("  (slow: acquires)" if d["slow"] else "")
                if d["axes"]:
                    extra += f"  array over {', '.join(d['axes'])}"
            else:
                extra = d.get("help", "")[:50]
            rows.append(f"  {d['kind']:9s} {pid:32s} {d['label'][:30]:30s} {extra}")
        return "\n".join(rows)

    # ---- set / get / read -------------------------------------------------

    def set(self, pid: str, value, timeout_s: float | None = None) -> float:
        """Set a parameter and BLOCK until it has settled. Returns the value.

        "Settled" is the module's own rule from its `describe` -- the magnet's
        field_stable, a stage's target echo -- exactly what a scan waits for.
        `timeout_s` overrides the module's settle timeout for this one call.
        Refuses a value outside the parameter's limits (nothing is sent).
        """
        p = self._param(pid)
        if p.kind != "settable":
            raise TypeError(f"{pid} is a detector (read-only); it cannot be set. "
                            f"Settable: lab.parameters('settable')")
        if isinstance(value, bool):
            value = float(value)
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise TypeError(f"{pid}: a number is needed, not {value!r}") from None
        if not math.isfinite(value):
            raise ValueError(f"{pid}: refusing to set {value}")
        self._refresh_limits()
        lo, hi = p.limits
        if not lo <= value <= hi:
            raise ValueError(f"{pid} = {value:g} {p.unit} is outside its limits "
                             f"{lo:g} .. {hi:g} {p.unit}; nothing was sent")
        self._claim([pid])
        self._log(f"set {pid} = {value:g} {p.unit}".rstrip())
        t0 = time.monotonic()
        try:
            p.set(value, timeout_s=timeout_s)
        except TimeoutError as exc:
            raise SettleTimeout(f"{pid} did not settle at {value:g} {p.unit}: "
                                f"{exc}") from exc
        except Exception as exc:
            raise self._refusal(exc, pid) or exc
        dt = time.monotonic() - t0
        if dt > 1.0:
            self._log(f"  {pid} settled after {dt:.1f} s")
        return value

    def get(self, pid: str):
        """One FRESH reading of a detector (or a knob's readback).

        A slow detector (one whose describe has an `acquire` block: a lock-in
        that must settle, a VNA sweep, a power meter averaging) is triggered
        and waited for, exactly as at a scan point. Anything else is read
        directly. Array detectors return a numpy array.
        """
        p = self._param(pid)
        spec = getattr(p, "acquire", None)
        if spec is None:
            return self._value(p.get())
        self._claim([pid])
        try:
            spec.trigger()
            spec.wait()
            return self._value(p.get())
        except TimeoutError as exc:
            raise SettleTimeout(f"{pid}: the acquisition did not finish: {exc}") from exc
        except Exception as exc:
            raise self._refusal(exc, pid) or exc

    def read(self, pid: str):
        """The CURRENT value from the status stream: no acquisition, no claim.

        For a slow detector this is its last acquisition (whenever that was),
        so use `get` for a measurement and `read` for watching.
        """
        return self._value(self._param(pid).get())

    # ---- actions -----------------------------------------------------------

    def run(self, action_id: str, **args):
        """Run an action (autofocus, take a reference ...) and wait until done.

        Raises ActionFailed if it failed or did not finish in its time -- a
        module that reports an outcome (the camera's autofocus) is checked.
        Keyword arguments replace the action's declared defaults.
        """
        a = self.registry.get_action(action_id)
        if a is None:
            self._no_action(action_id)
        self._claim([action_id])
        self._log(f"run {action_id}" + (f" {args}" if args else "") + " ...")
        t0 = time.monotonic()
        try:
            stem = f"{datetime.now().strftime('%H%M%S')}_{autosave.safe_name(self.name)}"
            reply = a.run(context={"data_dir": str(self.data_dir), "data_stem": stem,
                                   "moment": "script"}, args=args or None)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            refused = self._refusal(exc, action_id)
            if refused is not None:
                raise refused from exc
            raise ActionFailed(f"{action_id} failed: {exc}") from exc
        self._log(f"  {action_id} done after {time.monotonic() - t0:.1f} s")
        return reply

    # ---- waiting -----------------------------------------------------------

    def wait_until(self, condition, hold_s: float = 0.0,
                   timeout_s: float | None = 3600.0, poll_s: float = 0.5) -> float:
        """Block until `condition` is true -- and has STAYED true for `hold_s`.

        `condition` is a string -- "ppms.temperature_stable", "not
        clMag.locked", "ppms.temperature < 5.05" -- or any function of no
        arguments returning True/False (`lambda: abs(lab.read("t") - 5) < 0.02`).
        If it becomes false during the hold, the hold starts again.
        Raises WaitTimeout after `timeout_s` (None = wait for ever). Returns
        the seconds waited.
        """
        if callable(condition):
            pid, text = None, getattr(condition, "__name__", "condition")
            test = None
        elif isinstance(condition, str):
            pid, test = parse_condition(condition)
            self._param(pid)                  # a typo fails now, not in an hour
            text = condition.strip()
        else:
            raise TypeError("condition must be a string or a function")
        self._log(f"waiting until {text}" + (f" for {hold_s:g} s" if hold_s else ""))
        t0 = time.monotonic()
        since = None
        last_said = t0
        value = None
        while True:
            now = time.monotonic()
            if pid is None:
                ok = bool(condition())
            else:
                value = self.read(pid)
                ok = test(value)
            if ok:
                since = now if since is None else since
                if now - since >= hold_s:
                    self._log(f"  {text}: reached after {now - t0:.0f} s")
                    return now - t0
            else:
                since = None
            if timeout_s is not None and now - t0 >= timeout_s:
                last = "" if pid is None else f" (last value {pid} = {value!r})"
                held = "" if not hold_s else f" and stay so for {hold_s:g} s"
                raise WaitTimeout(f"waited {timeout_s:g} s for '{text}' to become "
                                  f"true{held}; it did not{last}")
            if now - last_said >= 60.0:
                last_said = now
                shown = "" if pid is None else f" ({pid} = {value!r})"
                self._log(f"  still waiting for {text}{shown}, {now - t0:.0f} s so far")
            time.sleep(poll_s)

    # ---- scans -------------------------------------------------------------

    def scan(self, recipe, name: str | None = None, comment: str | None = None,
             save: bool = True, data_dir=None, **meta):
        """Run one scan, save it like the suite does, return the xarray Dataset.

        `recipe`: a Recipe, a path to a .yaml recipe (or to a measured .nc,
        whose definition is reused), or a dict. `name` and `comment` replace
        the recipe's. The file is <data_dir>/<date>/<HHMMSS>_<name>.nc, written
        at checkpoints during a long scan and at the end; `api.path_of(ds)`
        (or `lab.last_path`) gives it. Extra keywords are stored as file
        attributes (`temperature_K=5`).

        Ctrl+C aborts cleanly: the points so far are saved, then
        KeyboardInterrupt ends the script.
        """
        r = self._recipe(recipe)
        if name is not None:
            r.name = str(name)
        if comment is not None:
            r.comment = str(comment)
        attrs = self._meta_attrs(meta)
        self._refresh_limits()
        errs = r.validate(self.registry)
        if errs:
            raise RecipeInvalid(f"scan '{r.name}' is not valid -- nothing was moved:"
                                "\n  - " + "\n  - ".join(errs))
        total = int(r.compile(self.registry).n_points)

        path = None
        if save:
            ddir = Path(data_dir) if data_dir else self.data_dir
            ok, msg = autosave.probe_save_target(ddir)
            if not ok:
                raise CannotSave(f"{msg} -- nothing was moved. Pass another "
                                 f"data_dir, or scan(..., save=False).")
            path = autosave.autosave_path(ddir, r.name)
        self._log(f"scan '{r.name}': {total} points"
                  + (f" -> {path}" if path else " (not saved)"))

        state = {"snapshot": None, "every": 0, "next": 0, "said": 0.0, "pct": 0}

        def write(ds):
            if ds is None:
                return
            ds.attrs.update(attrs)          # in memory too, saved or not
            if path is not None:
                autosave.write_dataset(ds, path)

        def on_point(done, n, snapshot):
            state["snapshot"] = snapshot
            # Checkpoints like the suite: every tenth of a scan longer than 100
            # points, so a crash costs at most a tenth of the measurement.
            if not state["every"]:
                state["every"] = max(1, n // 10) if n > 100 else 0
                state["next"] = state["every"]
            if state["every"] and done < n and done >= state["next"]:
                state["next"] = (done // state["every"] + 1) * state["every"]
                try:
                    write(snapshot())
                except Exception as exc:     # a full disk must not kill the scan
                    self._log(f"  could not save a checkpoint to {path}: {exc}")

        def on_progress(done, n, eta):
            pct = int(100 * done / max(1, n))
            now = time.monotonic()
            if done >= n or pct >= state["pct"] + 10 or now - state["said"] >= 60:
                state["pct"], state["said"] = pct - pct % 10, now
                self._log(f"  {r.name}: {done}/{n} points, about "
                          f"{_fmt_s(eta)} left")

        def keep(ds, why: str):
            """Save what was measured when the scan did not finish."""
            if ds is None:
                return
            try:
                write(ds)
                if path is not None:
                    self._log(f"  {why}: the points measured so far are saved in {path}")
            except Exception as exc:
                self._log(f"  {why}: could NOT save the measured points: {exc}")

        self._aborting = False
        old_handler = self._install_sigint()
        try:
            ds = _engine_run(r, self.registry, on_progress=on_progress,
                             should_abort=lambda: self._aborting,
                             created_iso=datetime.now().isoformat(timespec="seconds"),
                             on_point=on_point, on_log=lambda m: self._log(f"  {m}"),
                             data_path=path)
        except ControlRefused:
            raise
        except RoutineError as exc:
            keep(exc.dataset, "the after-scan routine failed")
            raise
        except ScanStopped as exc:
            # a ROUTINE stopped it (abort_if, a timed-out wait_until, Abort at
            # a pause) -- not Ctrl+C. Abort scan: hand back the measured part;
            # Abort all: ScanStoppedAll, so a script's loop / queue stops too.
            ds = getattr(exc, "dataset", None)
            keep(ds, f"stopped by a routine ({exc.reason})")
            if ds is not None and path is not None:
                ds.encoding["source"] = str(path)
                self.last_path = path
            if exc.whole_queue:
                err = ScanStoppedAll(f"scan '{r.name}' stopped -- ABORT ALL: {exc.reason}")
                err.dataset, err.path = ds, path
                raise err from None
            self._log(f"scan '{r.name}' stopped by a routine: {exc.reason}")
            return ds
        except ScanAborted as exc:
            keep(getattr(exc, "dataset", None), "aborted")
            raise self._interrupted(r.name, path, getattr(exc, "dataset", None)) from None
        except ScanFault as exc:
            keep(getattr(exc, "dataset", None), "stopped by a fault")
            raise
        except KeyboardInterrupt:
            # a SECOND Ctrl+C (or no handler could be installed): no after-scan
            # routine ran -- say so -- but keep what was measured
            snap = state["snapshot"]
            ds = None
            if snap is not None:
                try:
                    ds = snap()
                except Exception:
                    ds = None
            keep(ds, "stopped at once (Ctrl+C); the after-scan routine did NOT run")
            raise self._interrupted(r.name, path, ds) from None
        except Exception as exc:
            refused = self._refusal(exc, r.name)
            if refused is not None:
                raise refused from exc
            snap = state["snapshot"]
            if snap is not None:
                try:
                    keep(snap(), f"failed ({exc})")
                except Exception:
                    pass
            raise
        finally:
            self._restore_sigint(old_handler)

        try:
            write(ds)
        except Exception as exc:
            err = CannotSave(f"scan '{r.name}' finished but could NOT be saved to "
                             f"{path}: {exc}. The data is in exc.dataset.")
            err.dataset = ds
            raise err from exc
        if path is not None:
            ds.encoding["source"] = str(path)
            self.last_path = path
        if self._aborting:
            raise self._interrupted(r.name, path, ds)
        if ds.attrs.get("stopped_by"):
            # a routine stopped it between two points (the engine returned
            # normally): the same two outcomes as above
            if ds.attrs.get("stopped_scope") == "all":
                err = ScanStoppedAll(f"scan '{r.name}' stopped -- ABORT ALL: "
                                     f"{ds.attrs['stopped_by']}")
                err.dataset, err.path = ds, path
                raise err
            self._log(f"scan '{r.name}' stopped by a routine: {ds.attrs['stopped_by']}"
                      + (f", saved to {path}" if path else ""))
            return ds
        self._log(f"scan '{r.name}' done" + (f", saved to {path}" if path else ""))
        return ds

    def scan_queue(self, queue) -> list:
        """Run scans one after another: a saved queue file (.yaml from the
        Scan tab), or a list whose items are queue entries, Recipes, recipe
        dicts or recipe file paths.

        Returns the datasets of the scans that ran. Like the GUI queue: a
        routine's "Abort scan" goes on with the next scan, "Abort all"
        (ScanStoppedAll) ends the queue -- the datasets so far are on the
        exception as `.datasets`. Ctrl+C and an error also end it.
        """
        from . import scan_queue
        if not isinstance(queue, (list, tuple)):
            entries = scan_queue.load_queue_file(queue)
        else:
            entries = list(queue)
        out = []
        for k, e in enumerate(entries):
            if isinstance(e, scan_queue.QueueEntry):
                name, recipe = e.name, e.named_recipe()
            else:                                  # Recipe, dict or path: scan() reads it
                recipe = e
                name = getattr(e, "name", None) or (e.get("name") if isinstance(e, dict)
                                                    else str(e))
            self._log(f"queue: scan {k + 1} of {len(entries)} '{name}'")
            try:
                out.append(self.scan(recipe))
            except ScanStoppedAll as exc:
                exc.datasets = out + [exc.dataset]
                self._log(f"queue: ABORT ALL after scan {k + 1} -- "
                          f"{len(entries) - k - 1} scan(s) not run")
                raise
        return out

    # ---- internals ---------------------------------------------------------

    def _param(self, pid: str):
        p = self.registry.get(pid)
        if p is not None:
            return p
        if self.registry.get_action(pid) is not None:
            raise TypeError(f"{pid} is an action: run it with lab.run({pid!r})")
        known = self.parameters()
        close = difflib.get_close_matches(pid, known, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        where = " (simulated instruments have no module prefix)" \
            if self.connections is None and "." in pid else ""
        raise ParameterNotFound(f"no parameter called {pid!r}{where}.{hint} "
                                f"lab.parameters() lists all {len(known)}.")

    def _no_action(self, aid: str):
        if self.registry.get(aid) is not None:
            raise TypeError(f"{aid} is a parameter, not an action: use "
                            f"lab.set / lab.get")
        # an action the module has, but without a `wait` block: a script
        # could not tell when it has finished
        for name, inst in (self.connections.instruments.items()
                           if self.connections else []):
            man = getattr(inst, "manifest", None) or {}
            module = getattr(inst, "alias", None) or man.get("module", name)
            for d in man.get("parameters", []):
                if d.get("kind") == "action" and f"{module}.{d.get('id')}" == aid:
                    raise ActionFailed(
                        f"{aid} cannot be run from a script: its module does not "
                        f"say how to tell when it has finished (no `wait` block "
                        f"in its describe). Use its GUI.")
        known = self.parameters("action")
        close = difflib.get_close_matches(aid, known, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise ParameterNotFound(f"no action called {aid!r}.{hint} "
                                f"Actions: {', '.join(known) or 'none'}")

    @staticmethod
    def _value(v):
        if isinstance(v, np.generic):
            return v.item()
        return v

    def _owners(self, ids) -> set:
        owner = getattr(self.registry, "owner", None) or {}
        return {owner[i] for i in ids if i in owner}

    def _claim(self, ids, label: str | None = None) -> None:
        """Claim the instruments behind `ids` for this script (see module doc)."""
        if self.connections is None:
            return
        from .instrument import ScanBusy
        for n in sorted(self._owners(ids) - self._held - self._unprotected):
            inst = self.connections.instruments.get(n)
            if inst is None:
                continue
            try:
                ok = inst.claim_scan(label or self.name)
            except ScanBusy as exc:
                raise ControlRefused(
                    f"{n}: {exc}. A script follows the same rule as a scan: it "
                    f"needs control of the instrument. Ask whoever holds it to "
                    f"release it, or take control from a GUI on this PC.") from None
            if ok:
                self._held.add(n)
            else:
                self._unprotected.add(n)
                self._log(f"note: {n} is an older service that cannot be claimed; "
                          f"another scan could drive it at the same time")

    def _scan_claim(self, ids, label, on_log=None):
        """registry.scan_claim while this script owns the registry.

        The engine claims every instrument a scan uses before it moves
        anything. Here that goes through the script's own claims, so they are
        not given back at the end of the scan -- the next step of the script
        may need the same magnet."""
        self._claim(ids, label=f"{self.name}: {label}")
        return lambda: None

    def _refusal(self, exc, what: str):
        """A ControlRefused for a refusal by the control gate, else None."""
        from .instrument import ScanBusy
        text = str(exc)
        if isinstance(exc, ScanBusy) or "read-only:" in text or "busy: scan" in text:
            return ControlRefused(f"{what}: {text}")
        return None

    def _refresh_limits(self) -> None:
        if self.connections is not None:
            self.connections.refresh_stale(self.registry, prefix=True,
                                           on_warn=lambda m: self._log(f"note: {m}"))

    def _recipe(self, recipe) -> Recipe:
        if isinstance(recipe, Recipe):
            return Recipe.from_dict(copy.deepcopy(recipe.to_dict()))
        if isinstance(recipe, dict):
            return Recipe.from_dict(copy.deepcopy(recipe))
        if isinstance(recipe, (str, Path)):
            from .scan_queue import recipe_from_file
            p = Path(recipe)
            if not p.exists():
                raise FileNotFoundError(f"no recipe file {p} (relative paths start "
                                        f"in {Path.cwd()})")
            return recipe_from_file(p)
        raise TypeError("recipe must be a Recipe, a dict or a path to a .yaml / .nc")

    @staticmethod
    def _meta_attrs(meta: dict) -> dict:
        """Extra keywords of scan() as netCDF-safe file attributes."""
        out = {"saved_by": "scan_core.api", "script": _script_name()}
        for k, v in meta.items():
            if k in _RESERVED_ATTRS:
                raise ValueError(f"{k!r} is written by the engine itself; "
                                 f"use another name for your attribute")
            if isinstance(v, np.generic):
                v = v.item()
            if isinstance(v, bool):
                v = int(v)
            if not isinstance(v, (int, float, str)):
                v = json.dumps(v, default=str)
            out[k] = v
        return out

    def _install_sigint(self):
        """Ctrl+C during a scan = Abort (first press) / stop at once (second)."""
        if threading.current_thread() is not threading.main_thread():
            return None

        def handler(signum, frame):
            if self._aborting:
                raise KeyboardInterrupt
            self._aborting = True
            self._log("Ctrl+C: aborting the scan after this point (the measured "
                      "points will be saved) -- press Ctrl+C again to stop at once")
        try:
            return signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):
            return None

    @staticmethod
    def _restore_sigint(old) -> None:
        if old is not None:
            try:
                signal.signal(signal.SIGINT, old)
            except (ValueError, OSError):
                pass

    def _interrupted(self, name, path, ds):
        self._aborting = False
        msg = f"scan '{name}' aborted by Ctrl+C" + (f"; saved to {path}" if path and ds is not None else "")
        err = KeyboardInterrupt(msg)
        err.dataset, err.path = ds, path
        if path is not None and ds is not None:
            ds.encoding["source"] = str(path)
            self.last_path = path
        return err


def _fmt_s(seconds) -> str:
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 60}m {s % 60:02d}s"
