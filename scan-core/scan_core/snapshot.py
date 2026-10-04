"""snapshot.py -- what every instrument was set to when a scan ran, and recall.

Lukas (2026-10-04): "Instrument snapshots... available settings of each
instrument saved with data. Also a way how to set it back - an options list
listing differences between current and saved set and options to select what
to recall."

A scan file already says what was SCANNED (the recipe) and under which fixed
CONDITIONS. What it did not say is everything else: the lock-in's time
constant nobody touched, the camera's exposure, the stage's speed preset. Six
months later "why does this map look different?" is usually one of those.

So, right before the first point, the engine asks every connected instrument
for three things and files them in the netCDF, one JSON attribute per
instrument (`snapshot_<slug>`):

* ``config``  -- `get_config`: the module's SETTINGS (grouped, as the module
  keeps them in its .ini). These, and only these, can be recalled later.
* ``status``  -- the published state (positions, readings, flags), for
  information. Big arrays (a trace, a frame) are replaced by "<array n=...>".
* ``info``    -- the module's `info` block (limits, the instrument's idn).

plus the module name and `describe` revision, so a recall never sends one
module's settings to another.

This file is pure Python (no Qt, no ZeroMQ import): the engine, the recall
dialog and the tests all use it.

    take_snapshot(lab)              -> {slug: {...}}          (engine, at start)
    snapshot_attrs(snap, time)      -> {attr: str}            (into ds.attrs)
    read_snapshot(ds_or_path)       -> {slug: {...}}          (recall dialog)
    diff_config(saved, current)     -> [(path, saved, current)]
    partial_config(paths, saved)    -> {group: {key: value}}  (one set_config)

PRIVACY: a data file is private (the lab's), but the instrument's idn may
carry its serial number. The suite setting ``snapshot_include_idn`` (default
True, in suite_local.json) set to false drops every idn / serial entry. The
service's list of connected clients (PC and user names) is never stored.
"""

from __future__ import annotations

import functools
import json
import math
import platform
import subprocess
from datetime import datetime
from pathlib import Path

#: An array with more elements than this is not stored, only its size.
MAX_ELEMENTS = 1000
#: A text longer than this (a base64 frame...) is not stored, only its length.
MAX_TEXT = 4000
#: Attribute names in the netCDF file.
ATTR_PREFIX = "snapshot_"
ATTR_MODULES = "snapshot_modules"      # comma-separated slugs (a one-element
                                       # list attribute reads back as a plain
                                       # string, so a list would be ambiguous)
ATTR_TIME = "snapshot_time"
ATTR_END = "snapshot_end"              # settings that CHANGED during the scan
#: Keys dropped everywhere when snapshot_include_idn is false.
IDENTITY_KEYS = frozenset({"idn", "serial", "serial_number", "identity",
                           "device_id"})
#: Status keys never stored: who is connected (PC + user names), not the
#: instrument's state.
SESSION_KEYS = frozenset({"control", "clients"})
#: Seconds one instrument may take per request while a snapshot is taken.
REQUEST_TIMEOUT_MS = 2000
#: Two floats closer than this (relative) are "the same setting": a value
#: that went through the .ini and JSON may come back a rounding error away.
REL_TOL = 1e-12


class _Missing:
    """The marker for "this key does not exist on that side" in a diff."""
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return "<missing>"

    def __bool__(self):
        return False


MISSING = _Missing()


# ---------------------------------------------------------------- bounding

def _count(value) -> int:
    """Number of leaf elements in a (nested) list."""
    if isinstance(value, (list, tuple)):
        return sum(_count(v) for v in value)
    return 1


def bounded(value, drop_keys=frozenset()):
    """A JSON-safe copy of `value`, small enough for a file attribute.

    * a list with more than MAX_ELEMENTS leaves -> "<array n=N>"
    * a text longer than MAX_TEXT -> "<text n=N>"
    * NaN / inf -> "nan" / "inf" / "-inf" (strict JSON has no NaN, and MATLAB's
      jsondecode refuses it)
    * dict keys in `drop_keys` are left out; anything not JSON-native -> str
    """
    if isinstance(value, dict):
        return {str(k): bounded(v, drop_keys) for k, v in value.items()
                if str(k) not in drop_keys}
    if isinstance(value, (list, tuple)):
        n = _count(value)
        if n > MAX_ELEMENTS:
            return f"<array n={n}>"
        return [bounded(v, drop_keys) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else f"<text n={len(value)}>"
    # numpy scalars and anything else: try a number first, else its text
    try:
        return bounded(value.item(), drop_keys)
    except Exception:
        return bounded(str(value), drop_keys)


# ---------------------------------------------------------------- taking one

def include_idn_setting(root=None) -> bool:
    """The suite setting `snapshot_include_idn` (default True)."""
    try:
        from suite_common import get_setting
        v = get_setting("snapshot_include_idn", True, root=root)
    except Exception:
        return True
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(v)


def instrument_snapshot(inst, include_idn: bool = True,
                        include_status: bool = True,
                        slug: str | None = None) -> dict:
    """One instrument's entry. Never raises: what fails is recorded instead.

    `inst` is a scan_core.instrument.Instrument (anything with `command`,
    `status`, `name` and optionally `manifest` works -- the tests use fakes).
    """
    manifest = getattr(inst, "manifest", None) or {}
    drop = frozenset() if include_idn else IDENTITY_KEYS
    out = {"module": manifest.get("module") or getattr(inst, "name", ""),
           "revision": manifest.get("revision"),
           "label": manifest.get("label", "")}
    if slug:
        out["slug"] = slug
    errors = []
    try:
        cfg = inst.command("get_config", _timeout_ms=REQUEST_TIMEOUT_MS).get("config")
        out["config"] = bounded(cfg if isinstance(cfg, dict) else {}, drop)
    except Exception as exc:
        errors.append(f"get_config: {exc}")
    if include_status:
        try:
            st = inst.status() or {}
            out["status"] = bounded(st, drop | SESSION_KEYS)
        except Exception as exc:
            errors.append(f"status: {exc}")
        try:
            info = inst.command("info", _timeout_ms=REQUEST_TIMEOUT_MS).get("info")
            out["info"] = bounded(info if isinstance(info, dict) else {}, drop)
        except Exception as exc:
            errors.append(f"info: {exc}")
    if errors:
        out["error"] = "; ".join(errors)
    return out


def take_snapshot(lab, include_idn: bool | None = None,
                  include_status: bool = True) -> dict:
    """{slug: entry} for EVERY instrument the Lab is connected to.

    Not only the ones the recipe uses: the setting that spoils a map is
    usually on an instrument nobody thought was involved. One failing
    instrument gives `{"error": ...}` in its entry and never stops the others
    (or the scan).
    """
    from .lab import module_prefix             # lab imports zmq; keep it lazy
    if include_idn is None:
        include_idn = include_idn_setting()
    snap = {}
    for name, inst in list(getattr(lab, "instruments", {}).items()):
        try:
            slug = module_prefix(inst)
        except Exception:
            slug = name
        try:
            snap[slug] = instrument_snapshot(inst, include_idn=include_idn,
                                             include_status=include_status,
                                             slug=slug)
        except Exception as exc:               # belt and braces: never raise
            snap[slug] = {"module": name, "error": str(exc)}
    return snap


# ---------------------------------------------------------------- provenance

@functools.lru_cache(maxsize=None)
def _git_commit(root: str) -> str:
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--short=12", "HEAD"],
                           capture_output=True, text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _suite_root(root=None) -> Path:
    if root is not None:
        return Path(root)
    try:
        from suite_common import default_root
        return Path(default_root())
    except Exception:
        return Path(__file__).resolve().parents[2]


def provenance(root=None) -> dict:
    """Which software made the file: {attr: str}, empty values left out.

    `aaltoflow_version` = the git commit of the checkout (an installed copy
    has no .git; then a VERSION.txt in the root if there is one, else
    nothing). Plus scan-core's version, Python's, and this PC's setup name.
    """
    root_p = _suite_root(root)
    commit = _git_commit(str(root_p))
    if not commit:
        for name in ("VERSION.txt", "VERSION", "version.txt"):
            f = root_p / name
            try:
                if f.is_file():
                    commit = f.read_text(encoding="utf-8").strip().splitlines()[0][:80]
                    break
            except Exception:
                pass
    try:
        from importlib.metadata import version
        sc_version = version("scan-core")
    except Exception:
        sc_version = ""
    try:
        from suite_common import setup_name
        setup = setup_name(root)
    except Exception:
        setup = ""
    out = {"aaltoflow_version": commit, "software_scan_core": sc_version,
           "software_python": platform.python_version(), "setup_name": setup}
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- file side

def snapshot_attrs(snap: dict, when: str | None = None) -> dict:
    """The dataset attributes for a snapshot: one JSON string per instrument."""
    attrs = {f"{ATTR_PREFIX}{slug}": json.dumps(entry, sort_keys=True)
             for slug, entry in snap.items()}
    attrs[ATTR_MODULES] = ",".join(snap)
    attrs[ATTR_TIME] = when or datetime.now().isoformat(timespec="seconds")
    return attrs


def _attrs_of(source) -> dict:
    if isinstance(source, dict):
        return source
    if hasattr(source, "attrs"):
        return dict(source.attrs)
    import xarray as xr
    with xr.open_dataset(source) as ds:
        return dict(ds.attrs)


def read_snapshot(source) -> dict:
    """{slug: entry} from a Dataset, a path to a .nc, or an attrs dict.

    {} for a file written before snapshots existed. An attribute that is not
    valid JSON gives {"error": ...} for that instrument instead of raising.
    """
    attrs = _attrs_of(source)
    reserved = {ATTR_MODULES, ATTR_TIME, ATTR_END}
    out = {}
    for key, val in attrs.items():
        if not key.startswith(ATTR_PREFIX) or key in reserved:
            continue
        slug = key[len(ATTR_PREFIX):]
        try:
            entry = json.loads(val)
            if not isinstance(entry, dict):
                raise ValueError("not an object")
        except Exception as exc:
            entry = {"error": f"unreadable snapshot ({exc})"}
        out[slug] = entry
    return out


# ---------------------------------------------------------------- diff + recall

def _flatten(d, prefix=()):
    """{path tuple: leaf}. Dicts are walked; lists and scalars are leaves
    (a list is one setting, e.g. a per-axis step size, and is sent whole)."""
    out = {}
    if isinstance(d, dict) and d:
        for k, v in d.items():
            out.update(_flatten(v, prefix + (str(k),)))
    elif prefix:
        out[prefix] = d
    return out


def same_value(a, b, rel_tol: float = REL_TOL) -> bool:
    """Equal as SETTINGS: floats within rel_tol, lists element by element,
    bool never equal to a number (a type change is a difference)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    num = (int, float)
    if isinstance(a, num) and isinstance(b, num):
        if a == b:
            return True
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=0.0)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same_value(x, y, rel_tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return (a.keys() == b.keys()
                and all(same_value(a[k], b[k], rel_tol) for k in a))
    return type(a) is type(b) and a == b


def diff_config(saved: dict, current: dict, rel_tol: float = REL_TOL) -> list:
    """[(path, saved_value, current_value)] for every setting that differs.

    `path` is a tuple ("motion", "speed_fast"); a key present on one side only
    has MISSING on the other. Sorted by path, so the dialog is stable.
    """
    fs, fc = _flatten(saved or {}), _flatten(current or {})
    out = []
    for path in sorted(set(fs) | set(fc)):
        a, b = fs.get(path, MISSING), fc.get(path, MISSING)
        if a is MISSING or b is MISSING or not same_value(a, b, rel_tol):
            out.append((path, a, b))
    return out


def all_settings(saved: dict, current: dict) -> list:
    """Like diff_config but EVERY setting (the dialog's "show all")."""
    fs, fc = _flatten(saved or {}), _flatten(current or {})
    return [(p, fs.get(p, MISSING), fc.get(p, MISSING))
            for p in sorted(set(fs) | set(fc))]


def recallable(saved_value, current_value) -> bool:
    """A setting can be sent back only if the file has it AND the module still
    has it (a key the module no longer knows would be silently ignored), and
    the file did not shorten it to "<array n=...>"/"<text n=...>"."""
    if saved_value is MISSING or current_value is MISSING:
        return False
    if isinstance(saved_value, str) and saved_value.startswith(("<array n=", "<text n=")):
        return False
    return True


def partial_config(paths, saved: dict) -> dict:
    """The nested dict ONE set_config needs for exactly these paths.

    Only what was ticked goes out -- never the whole saved config: a module's
    set_config overwrites every key it is given (gotcha #5, e.g. the "zero
    here" origin of a stage lives in its config).
    """
    flat = _flatten(saved or {})
    out: dict = {}
    for path in paths:
        path = tuple(path)
        if path not in flat:
            raise KeyError(f"{'.'.join(path)} is not in the saved settings")
        node = out
        for k in path[:-1]:
            node = node.setdefault(k, {})
        node[path[-1]] = flat[path]
    return out


def path_text(path) -> str:
    return ".".join(path)


def value_text(value, width: int = 60) -> str:
    """A setting as one short line for the dialog."""
    if value is MISSING:
        return "(not there)"
    try:
        text = json.dumps(value) if not isinstance(value, str) else value
    except Exception:
        text = str(value)
    return text if len(text) <= width else text[:width - 3] + "..."
