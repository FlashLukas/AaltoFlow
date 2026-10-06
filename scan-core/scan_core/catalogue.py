"""
catalogue.py -- a searchable index of every run in the data folder (2026-10-04).

The question it answers: "which scans did we take on sample B7 at 5 K last
month, and where are they?" Before this, the only way was to open the files
one by one in the Data tab.

THE FILES ARE THE TRUTH; THE INDEX IS DISPOSABLE. Everything here is read FROM
the .nc files -- their attributes, their axes, the names of their variables --
into one SQLite file, `catalogue.sqlite`, in the data folder. Nothing is ever
written INTO a data file, and nothing exists only in the index. Delete the
index at any time; the next scan rebuilds it. Move the data folder and it comes
along (paths are stored relative to the folder).

What a scan reads, per file -- ATTRIBUTES ONLY, never the measured arrays (a
year of 2-D maps would take minutes to load and the index needs none of it):
  * the run: name, created, seconds (duration), n_points, comment, the recipe's
    name, a complete/aborted flag if the file has one, file size;
  * the axes: name, length, coordinate range and units (an index coordinate is
    one short 1-D array -- that IS read; it is what "field 0..100 mT" needs);
  * the detectors: variable names, units, `aaltoflow_type` (storage.py);
  * the conditions: the scalar "fixed" coordinates (rf_power = 5 dBm ...);
  * the run info (written by the suite since the snapshots branch; absent in
    older files, which is fine): sample, structure, operator, project, tags,
    series, setup_name, aaltoflow_version, snapshot_time, snapshot_modules;
  * the instrument snapshots `snapshot_<slug>` (JSON: settings + status),
    FLATTENED into key/value rows so they can be searched:
    `ppms.status.temperature = 5.02`.

Incremental: a rescan re-reads only files whose size or modification time
changed, and drops the entries of files that are gone. A file that cannot be
read is recorded WITH its error (the table shows it) and the scan goes on.

Search (`search(...)`) combines free text, simple field filters, a date range
and a small `where` language over the conditions and snapshot values:

    ppms.temperature between 4 and 6
    clMag.field == 50 and rf_power > 0
    ppms.mode == "persistent"
    n_points >= 100

The `where` text is PARSED here (a tokenizer + a tiny grammar), never pasted
into SQL: keys must look like names, and every value goes to SQLite as a bound
parameter. Text such as `x == 1; DROP TABLE files` is a syntax error, not a
query.

Command line (for scripts):

    uv run python -m scan_core.catalogue scan [DATA_DIR]
    uv run python -m scan_core.catalogue search [DATA_DIR] --sample B7 \
        --where "ppms.temperature between 4 and 6" [--json]

No Qt here: the suite's Catalogue tab (apps/catalogue_view.py) is a view over
this module, and the tests drive it without a screen.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

#: the index file, inside the data folder
INDEX_NAME = "catalogue.sqlite"

#: bump when the tables change: an index of another version is simply rebuilt
#: (it is disposable -- the files are the truth)
SCHEMA_VERSION = 1

#: run-info attributes copied verbatim into their own columns
RUN_INFO = ("sample", "structure", "operator", "comment", "project", "series",
            "setup_name", "aaltoflow_version", "snapshot_time")

#: keys a `where` term may name that are columns of the run itself
_RUN_COLUMNS = {"n_points": "n_points", "duration": "duration",
                "seconds": "duration", "size": "size", "size_bytes": "size"}

#: a flattened snapshot never yields more rows than this per instrument (a
#: module that dumps a 4000-entry table must not swamp the index)
MAX_VALUES_PER_SNAPSHOT = 2000
#: lists up to this length are indexed element by element (key.0, key.1 ...)
MAX_LIST_ITEMS = 32

_SCHEMA = """
CREATE TABLE files (
    id            INTEGER PRIMARY KEY,
    relpath       TEXT UNIQUE NOT NULL,
    size          INTEGER,
    mtime_ns      INTEGER,
    error         TEXT,
    name          TEXT,
    recipe_name   TEXT,
    created       TEXT,
    duration      REAL,
    n_points      INTEGER,
    status        TEXT,
    dims_text     TEXT,
    dims_json     TEXT,
    detectors_text TEXT,
    instruments_text TEXT,
    tags_text     TEXT,
    sample TEXT, structure TEXT, operator TEXT, comment TEXT, project TEXT,
    series TEXT, setup_name TEXT, aaltoflow_version TEXT, snapshot_time TEXT,
    indexed_at    TEXT
);
CREATE TABLE detectors (file_id INTEGER, name TEXT, units TEXT, type TEXT, label TEXT);
CREATE TABLE tags (file_id INTEGER, tag TEXT);
CREATE TABLE instruments (file_id INTEGER, slug TEXT, source TEXT);
CREATE TABLE conditions (file_id INTEGER, key TEXT, num_value REAL,
                         text_value TEXT, units TEXT);
CREATE TABLE snapshot_values (file_id INTEGER, key TEXT, num_value REAL,
                              text_value TEXT);
CREATE INDEX ix_det ON detectors(file_id);
CREATE INDEX ix_det_name ON detectors(name);
CREATE INDEX ix_tags ON tags(tag);
CREATE INDEX ix_inst ON instruments(slug);
CREATE INDEX ix_cond ON conditions(key);
CREATE INDEX ix_snap ON snapshot_values(key);
CREATE INDEX ix_snap_file ON snapshot_values(file_id);
CREATE INDEX ix_created ON files(created);
"""

_CHILD_TABLES = ("detectors", "tags", "instruments", "conditions", "snapshot_values")


class WhereError(ValueError):
    """The `where` text could not be parsed. The message says where and why."""


# ═══════════════════════════════ the index file ═══════════════════════════════

def index_path(data_dir) -> Path:
    return Path(data_dir) / INDEX_NAME


def _connect(data_dir) -> sqlite3.Connection:
    """Open (and if needed create or rebuild) the index of `data_dir`.

    One connection per call, closed by the caller: the GUI scans in a worker
    thread and searches in the GUI thread, and a SQLite connection must not be
    shared between threads.
    """
    path = index_path(data_dir)
    con = sqlite3.connect(str(path), timeout=10)
    try:
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        has = con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                          "AND name='files'").fetchone()
    except sqlite3.DatabaseError:
        # not a SQLite file at all (a stray file of that name, a torn copy):
        # it is only an index -- start a new one
        con.close()
        path.unlink(missing_ok=True)
        con = sqlite3.connect(str(path), timeout=10)
        ver, has = 0, None
    if not has or ver != SCHEMA_VERSION:
        for t in ("files",) + _CHILD_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.executescript(_SCHEMA)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
    return con


# ═══════════════════════════════ reading one file ═════════════════════════════

def _plain(v):
    """An attribute value as plain Python (numpy scalars and arrays out)."""
    try:
        import numpy as np
        if isinstance(v, np.ndarray):
            return [_plain(x) for x in v.tolist()]
        if isinstance(v, np.generic):
            return v.item()
    except ImportError:                      # pragma: no cover
        pass
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def _text(v) -> str | None:
    v = _plain(v)
    if v is None:
        return None
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    s = str(v).strip()
    return s or None


def _as_list(v) -> list[str]:
    """A list attribute however it was written: a netCDF string array, a JSON
    list, or a comma-separated string ("a, b,c")."""
    v = _plain(v)
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        items = v
    else:
        s = str(v).strip()
        items = None
        if s.startswith("["):
            try:
                got = json.loads(s)
                if isinstance(got, list):
                    items = got
            except ValueError:
                pass
        if items is None:
            items = s.split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def _num(v) -> float | None:
    """A number if `v` is (or reads as) a finite one, else None. Bools count:
    true = 1, so "output == 1" and "output == true" both work."""
    v = _plain(v)
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _flatten(obj, prefix: str, out: list, limit: int) -> None:
    """Nested JSON -> [(dotted key, value)], scalars only."""
    if len(out) >= limit:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out, limit)
    elif isinstance(obj, (list, tuple)):
        if len(obj) <= MAX_LIST_ITEMS and all(
                not isinstance(x, (dict, list, tuple)) for x in obj):
            for i, x in enumerate(obj):
                _flatten(x, f"{prefix}.{i}", out, limit)
        else:
            out.append((prefix, json.dumps(obj)[:200]))
    else:
        out.append((prefix, obj))


def _status_of(attrs: dict) -> str | None:
    """complete / aborted / partial, if the file says so (older files do not)."""
    for key in ("outcome", "run_status", "status"):
        s = _text(attrs.get(key))
        if s:
            return s.lower()
    if "aborted" in attrs:
        a = _plain(attrs["aborted"])
        if str(a).lower() in ("1", "true", "yes"):
            return "aborted"
        if str(a).lower() in ("0", "false", "no"):
            return "complete"
    for key in ("complete", "completed"):
        if key in attrs:
            a = str(_plain(attrs[key])).lower()
            return "complete" if a in ("1", "true", "yes") else "partial"
    return None


def _iso(created, fallback_mtime: float) -> str:
    """`created` as 'YYYY-MM-DDTHH:MM:SS' (sortable text). An old or odd file
    without a usable timestamp gets its modification time instead -- a date
    range must not lose it."""
    s = _text(created)
    if s:
        try:
            return datetime.fromisoformat(s).replace(tzinfo=None).isoformat(
                timespec="seconds")
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback_mtime).isoformat(timespec="seconds")


def read_entry(path) -> dict:
    """Everything the catalogue keeps about one file, read WITHOUT loading the
    data. Raises whatever the reader raises for a broken file (scan() records
    it)."""
    import xarray as xr
    path = Path(path)
    st = path.stat()
    # decode_times off (a coordinate named "time" in seconds is not a date),
    # mask_and_scale off (no fill-value decoding needed for names and ranges),
    # cache off (nothing is kept). `with` closes the file: on Windows an open
    # handle would stop the scan builder from replacing that file next time.
    with xr.open_dataset(path, engine="h5netcdf", decode_times=False,
                         mask_and_scale=False, cache=False) as ds:
        attrs = {k: v for k, v in ds.attrs.items()}
        dims = []
        for d, n in ds.sizes.items():
            info = {"name": d, "size": int(n)}
            if d in ds.coords:
                c = ds.coords[d]
                info["units"] = _text(c.attrs.get("units")) or ""
                if c.attrs.get("param"):
                    info["param"] = _text(c.attrs.get("param"))
                try:
                    import numpy as np
                    vals = np.asarray(c.values)       # one short 1-D array
                    if vals.size and vals.dtype.kind in "iuf":
                        info["min"] = float(np.nanmin(vals))
                        info["max"] = float(np.nanmax(vals))
                except Exception:                   # a range is a nicety
                    pass
            dims.append(info)

        detectors, seen_pairs = [], set()
        for name, var in ds.data_vars.items():
            va = var.attrs
            pair = _text(va.get("complex_pair"))
            if pair:
                # <id>_real + <id>_imag are ONE complex detector
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                detectors.append({"name": pair, "units": _text(va.get("units")) or "",
                                  "type": "complex",
                                  "label": _text(va.get("label")) or pair})
                continue
            detectors.append({"name": str(name),
                              "units": _text(va.get("units")) or "",
                              "type": _text(va.get("aaltoflow_type")) or "",
                              "label": _text(va.get("label")) or str(name)})

        conditions = []
        for name, c in ds.coords.items():
            if c.ndim != 0:
                continue
            try:
                raw = _plain(c.values.item() if hasattr(c.values, "item") else c.values)
            except Exception:
                raw = None
            conditions.append({"key": str(name), "num": _num(raw),
                               "text": None if raw is None else str(raw),
                               "units": _text(c.attrs.get("units")) or ""})

    recipe_name = None
    rj = _text(attrs.get("recipe_json"))
    if rj:
        try:
            recipe_name = (json.loads(rj) or {}).get("name")
        except (ValueError, AttributeError):
            pass

    # the instrument snapshots: one JSON attribute per module
    snapshots = {}
    for key, raw in attrs.items():
        if not key.startswith("snapshot_") or key in ("snapshot_modules", "snapshot_time"):
            continue
        slug = key[len("snapshot_"):]
        try:
            snapshots[slug] = json.loads(_text(raw) or "null")
        except ValueError:
            snapshots[slug] = {"_unparsed": _text(raw)}
    instruments = [(s, "snapshot") for s in _as_list(attrs.get("snapshot_modules"))]
    for slug in snapshots:
        if slug not in [s for s, _ in instruments]:
            instruments.append((slug, "snapshot"))
    # an older file has no snapshot, but its parameter ids still name the
    # modules (lab registry ids are "<slug>.<id>"): better than nothing
    used = [d.get("param") or d["name"] for d in dims] + [d["name"] for d in detectors] \
        + [c["key"] for c in conditions]
    for pid in used:
        if "." in pid:
            slug = pid.split(".", 1)[0]
            if slug not in [s for s, _ in instruments]:
                instruments.append((slug, "recipe"))

    entry = {
        "size": st.st_size, "mtime_ns": st.st_mtime_ns, "error": None,
        "name": _text(attrs.get("name")) or path.stem,
        "recipe_name": recipe_name,
        "created": _iso(attrs.get("created"), st.st_mtime),
        "duration": _num(attrs.get("seconds")),
        "n_points": int(_num(attrs.get("n_points")) or 0) or None,
        "status": _status_of(attrs),
        "dims": dims, "detectors": detectors, "conditions": conditions,
        "tags": _as_list(attrs.get("tags")),
        "instruments": instruments, "snapshots": snapshots,
    }
    for k in RUN_INFO:
        entry[k] = _text(attrs.get(k))
    return entry


def _dims_text(dims: list) -> str:
    """"field(41) x rf_freq(101)" -- the shape at a glance."""
    return " x ".join(f"{d['name']}({d['size']})" for d in dims)


def _store(con, relpath: str, entry: dict) -> None:
    old = con.execute("SELECT id FROM files WHERE relpath=?", (relpath,)).fetchone()
    if old:
        _forget(con, old[0])
    dets = entry.get("detectors") or []
    tags = entry.get("tags") or []
    insts = entry.get("instruments") or []
    cols = {
        "relpath": relpath, "size": entry.get("size"), "mtime_ns": entry.get("mtime_ns"),
        "error": entry.get("error"), "name": entry.get("name"),
        "recipe_name": entry.get("recipe_name"), "created": entry.get("created"),
        "duration": entry.get("duration"), "n_points": entry.get("n_points"),
        "status": entry.get("status"),
        "dims_text": _dims_text(entry.get("dims") or []),
        "dims_json": json.dumps(entry.get("dims") or []),
        "detectors_text": ", ".join(d["name"] for d in dets),
        "instruments_text": ", ".join(s for s, _ in insts),
        "tags_text": ", ".join(tags),
        "indexed_at": datetime.now().isoformat(timespec="seconds"),
    }
    for k in RUN_INFO:
        cols[k] = entry.get(k)
    names = ", ".join(cols)
    marks = ", ".join("?" * len(cols))
    cur = con.execute(f"INSERT INTO files ({names}) VALUES ({marks})", list(cols.values()))
    fid = cur.lastrowid
    con.executemany("INSERT INTO detectors VALUES (?,?,?,?,?)",
                    [(fid, d["name"], d["units"], d["type"], d["label"]) for d in dets])
    con.executemany("INSERT INTO tags VALUES (?,?)",
                    [(fid, t.lower()) for t in dict.fromkeys(tags)])
    con.executemany("INSERT INTO instruments VALUES (?,?,?)",
                    [(fid, s, src) for s, src in insts])
    con.executemany("INSERT INTO conditions VALUES (?,?,?,?,?)",
                    [(fid, c["key"], c["num"], c["text"], c["units"])
                     for c in entry.get("conditions") or []])
    rows = []
    for slug, snap in (entry.get("snapshots") or {}).items():
        flat: list = []
        _flatten(snap, slug, flat, MAX_VALUES_PER_SNAPSHOT)
        for key, val in flat:
            val = _plain(val)
            rows.append((fid, key, _num(val),
                         None if val is None else (str(val).lower() if isinstance(val, bool)
                                                   else str(val))))
    con.executemany("INSERT INTO snapshot_values VALUES (?,?,?,?)", rows)


def _forget(con, fid: int) -> None:
    for t in _CHILD_TABLES:
        con.execute(f"DELETE FROM {t} WHERE file_id=?", (fid,))
    con.execute("DELETE FROM files WHERE id=?", (fid,))


# ═══════════════════════════════════ scan ═════════════════════════════════════

def data_files(data_dir) -> list[Path]:
    """Every .nc under `data_dir`, day folders included. Skips the scan
    builder's half-written temp files (`*.writing.nc`, renamed into place when
    complete) -- indexing one would catch it mid-write."""
    root = Path(data_dir)
    out = []
    for p in root.rglob("*.nc"):
        if p.name.endswith(".writing.nc") or not p.is_file():
            continue
        out.append(p)
    return sorted(out)


def scan(data_dir, progress: Callable | None = None,
         cancel: Callable[[], bool] | None = None) -> dict:
    """Bring the index of `data_dir` up to date with the files.

    progress(done, total, relpath) is called for every file that is (re)read;
    cancel() returning True stops between files (what is done stays done).
    Returns counts: {"files", "read", "unchanged", "removed", "errors",
    "seconds", "cancelled"}.
    """
    t0 = time.monotonic()
    root = Path(data_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"no such data folder: {root}")
    files = data_files(root)
    rel = {p.relative_to(root).as_posix(): p for p in files}
    counts = {"files": len(files), "read": 0, "unchanged": 0, "removed": 0,
              "errors": 0, "cancelled": False}
    with closing(_connect(root)) as con:
        known = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
            "SELECT relpath, size, mtime_ns, id FROM files")}
        # files that are gone: their entries go too
        for rp, (_s, _m, fid) in known.items():
            if rp not in rel:
                _forget(con, fid)
                counts["removed"] += 1
        con.commit()
        todo = []
        for rp, p in rel.items():
            try:
                st = p.stat()
            except OSError:
                continue
            k = known.get(rp)
            if k and k[0] == st.st_size and k[1] == st.st_mtime_ns:
                counts["unchanged"] += 1
            else:
                todo.append((rp, p))
        for i, (rp, p) in enumerate(todo):
            if cancel and cancel():
                counts["cancelled"] = True
                break
            try:
                entry = read_entry(p)
            except Exception as exc:          # one bad file never stops the scan
                try:
                    st = p.stat()
                    size, mt = st.st_size, st.st_mtime_ns
                except OSError:
                    size, mt = None, None
                entry = {"size": size, "mtime_ns": mt, "name": p.stem,
                         "created": datetime.fromtimestamp(
                             (mt or 0) / 1e9).isoformat(timespec="seconds"),
                         "error": f"{type(exc).__name__}: {exc}"[:500]}
                counts["errors"] += 1
            _store(con, rp, entry)
            counts["read"] += 1
            if i % 50 == 49:
                con.commit()                  # progress survives an interruption
            if progress:
                progress(i + 1, len(todo), rp)
        con.commit()
    counts["seconds"] = time.monotonic() - t0
    return counts


# ════════════════════════════════ the where language ══════════════════════════

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(?![\w.])
    | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<op>==|!=|<=|>=|=|<|>|~)
    | (?P<comma>,)
    | (?P<word>[A-Za-z_][\w.\-:/@\[\]]*)
    )""", re.VERBOSE)

_KEY = re.compile(r"^[A-Za-z_][\w.\-:/@\[\]]*$")


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise WhereError(f"cannot read {text[pos:pos + 20]!r} "
                             f"(position {pos + 1})")
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "str":
            val = re.sub(r"\\(.)", r"\1", val[1:-1])
        out.append((kind, val))
        pos = m.end()
    return out


def parse_where(text: str) -> list[tuple]:
    """'a.b between 1 and 2, c == x' -> [("a.b", "between", 1.0, 2.0),
    ("c", "==", "x")]. Terms are joined by `and` or a comma (all must hold).
    Operators: == = != < <= > >= , `between A and B` (inclusive), and
    `contains` / `~` (text contains, case-insensitive).
    A NAME ALONE ("kim", "ppms.temperature", "rf_power") -> (key, "exists"):
    the run has a condition, a snapshot value or an instrument of that name.
    Typing a module's name to find its runs is the first thing people try,
    and "expected an operator" helped nobody (Lukas, 2026-10-06)."""
    toks = _tokens(text or "")
    terms, i = [], 0

    def need(cond, what):
        if not cond:
            got = toks[i][1] if i < len(toks) else "the end"
            raise WhereError(f"expected {what}, found {got!r}")

    def value():
        nonlocal i
        need(i < len(toks) and toks[i][0] in ("num", "str", "word"), "a value")
        kind, val = toks[i]; i += 1
        return float(val) if kind == "num" else val

    while i < len(toks):
        need(toks[i][0] == "word" and _KEY.match(toks[i][1]), "a name")
        key = toks[i][1]; i += 1
        if i == len(toks) or toks[i][0] == "comma" or toks[i][1].lower() == "and":
            # a NAME ALONE: "the run has it" (see the docstring)
            terms.append((key, "exists"))
            kind = low = None
        else:
            kind, op = toks[i]
            low = op.lower()
        if kind is None:
            pass
        elif kind == "word" and low == "between":
            i += 1
            lo = value()
            need(i < len(toks) and toks[i][1].lower() == "and", "'and' (between A and B)")
            i += 1
            hi = value()
            if not isinstance(lo, float) or not isinstance(hi, float):
                raise WhereError(f"between needs two numbers ({key})")
            terms.append((key, "between", min(lo, hi), max(lo, hi)))
        elif kind == "op" or (kind == "word" and low == "contains"):
            i += 1
            op = {"=": "==", "contains": "~"}.get(low, op)
            terms.append((key, op, value()))
        else:
            need(False, "an operator (==, !=, <, <=, >, >=, between, contains)")
        if i < len(toks):
            need(toks[i][0] == "comma" or toks[i][1].lower() == "and",
                 "'and' or ',' between conditions")
            i += 1
            need(i < len(toks), "another condition after 'and'")
    return terms


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: two floats are "equal" for == when they agree to this relative tolerance
#: (a field stored as 49.99999999 is 50 for anyone typing 50)
_REL_TOL = 1e-6


def _value_sql(op: str, args: tuple, col_num: str, col_text: str) -> tuple[str, list]:
    if op == "between":
        return f"{col_num} BETWEEN ? AND ?", [args[0], args[1]]
    v = args[0]
    if isinstance(v, float):
        tol = _REL_TOL * max(1.0, abs(v))
        if op == "==":
            return f"ABS({col_num} - ?) <= ?", [v, tol]
        if op == "!=":
            return f"ABS({col_num} - ?) > ?", [v, tol]
        if op == "~":
            return f"{col_text} LIKE ? ESCAPE '\\'", [f"%{_like_escape(_fmt(v))}%"]
        return f"{col_num} {op} ?", [v]          # op is from a fixed set
    # text
    if op == "==":
        return f"{col_text} = ? COLLATE NOCASE", [v]
    if op == "!=":
        return f"{col_text} <> ? COLLATE NOCASE", [v]
    if op == "~":
        return f"{col_text} LIKE ? ESCAPE '\\'", [f"%{_like_escape(v)}%"]
    raise WhereError(f"{op} needs a number, not {v!r}")


def _fmt(v: float) -> str:
    return str(int(v)) if v == int(v) else repr(v)


def _term_sql(term: tuple) -> tuple[str, list]:
    """One where-term -> 'files.id IN (...)' with its parameters.

    A key is looked up in three places, any of which may match:
      * a run column (n_points, duration, size);
      * a CONDITION of the scan (a fixed coordinate), by its exact id;
      * a SNAPSHOT value: `slug.key` matches the flattened key exactly, or with
        any path in between -- `ppms.temperature` finds
        `ppms.status.temperature` (and `ppms.settings.x.temperature`, if a
        module had both: either matching is enough).
    """
    key, op, *args = term
    args = tuple(args)
    if op == "exists":
        return _exists_sql(key)
    if key.lower() in _RUN_COLUMNS:
        col = _RUN_COLUMNS[key.lower()]
        sql, p = _value_sql(op, args, f"files.{col}", f"CAST(files.{col} AS TEXT)")
        return sql, p
    c_sql, c_p = _value_sql(op, args, "num_value", "text_value")
    parts = [f"SELECT file_id FROM conditions WHERE key = ? COLLATE NOCASE AND {c_sql}"]
    params = [key] + c_p
    if "." in key:
        slug, rest = key.split(".", 1)
        deep = f"{_like_escape(slug)}.%.{_like_escape(rest)}"
        parts.append("SELECT file_id FROM snapshot_values WHERE "
                     f"(key = ? COLLATE NOCASE OR key LIKE ? ESCAPE '\\') AND {c_sql}")
        params += [key, deep] + c_p
    return "files.id IN (" + " UNION ".join(parts) + ")", params


def _exists_sql(key: str) -> tuple[str, list]:
    """A bare name in `where`: the run HAS it. Matches a run column that is
    filled in, an instrument slug ("kim"), a condition, or a snapshot key --
    exactly, as a prefix ("kim" -> "kim.status.position_x") or with a path in
    between ("ppms.temperature" -> "ppms.status.temperature")."""
    if key.lower() in _RUN_COLUMNS:
        return f"files.{_RUN_COLUMNS[key.lower()]} IS NOT NULL", []
    prefix = f"{_like_escape(key)}.%"
    parts = ["SELECT file_id FROM instruments WHERE slug = ? COLLATE NOCASE",
             "SELECT file_id FROM conditions WHERE key = ? COLLATE NOCASE "
             "OR key LIKE ? ESCAPE '\\'",
             "SELECT file_id FROM snapshot_values WHERE key = ? COLLATE NOCASE "
             "OR key LIKE ? ESCAPE '\\'"]
    params = [key, key, prefix, key, prefix]
    if "." in key:
        slug, rest = key.split(".", 1)
        parts.append("SELECT file_id FROM snapshot_values WHERE key LIKE ? ESCAPE '\\'")
        params.append(f"{_like_escape(slug)}.%.{_like_escape(rest)}")
    return "files.id IN (" + " UNION ".join(parts) + ")", params


# ════════════════════════════════════ search ══════════════════════════════════

def _day(v, end: bool = False) -> str | None:
    """A date bound as sortable ISO text. A bare date as `date_to` means the
    WHOLE day (everything before the next midnight)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, date):
        d = v + timedelta(days=1) if end else v
        return datetime(d.year, d.month, d.day).isoformat(timespec="seconds")
    s = str(v).strip()
    if len(s) == 10:                          # YYYY-MM-DD
        return _day(date.fromisoformat(s), end)
    return datetime.fromisoformat(s).isoformat(timespec="seconds")


def search(data_dir, text=None, sample=None, operator=None, project=None,
           tags=None, series=None, instrument=None, detector=None, setup=None,
           structure=None, name=None, axis=None,
           date_from=None, date_to=None, where=None, limit: int | None = None,
           include_errors: bool = True) -> list[dict]:
    """Find runs in the index of `data_dir`, newest first.

    text      : words; EVERY word must appear somewhere in name, sample,
                structure, comment, tags or the file name (case-insensitive)
    sample, operator, project, series, structure, name : "contains", any case
    axis      : a scan axis (dimension) name contains this text ("field"
                finds field and field_x; "kim" finds kim.position_x)
    setup     : the SETUP the file was measured on (attribute setup_name, set by
                the installer: "TR-MOKE", "VNA-FMR" ...); "contains", any case
    tags      : a list or "a, b": the run must carry EVERY tag (exact, any case)
    instrument: a module slug (from the snapshot, or from the parameter ids of
                an older file)
    detector  : a detector id; "r1" also finds "hf2.r1"
    date_from, date_to : date / datetime / "YYYY-MM-DD"; date_to inclusive
    where     : see parse_where -- conditions and snapshot values
    Rows are dicts; `path` is absolute, `dims` and `detectors` are decoded.
    Raises WhereError for a `where` it cannot read.
    """
    sql = ["SELECT files.* FROM files WHERE 1=1"]
    params: list = []

    for word in (text or "").split():
        like = f"%{_like_escape(word)}%"
        sql.append("AND (" + " OR ".join(
            f"COALESCE({c},'') LIKE ? ESCAPE '\\'"
            for c in ("name", "sample", "structure", "comment", "tags_text",
                      "relpath")) + ")")
        params += [like] * 6
    for col, val in (("sample", sample), ("operator", operator),
                     ("project", project), ("series", series),
                     ("setup_name", setup), ("structure", structure),
                     ("name", name)):
        if val:
            sql.append(f"AND COALESCE({col},'') LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(str(val).strip())}%")
    if axis:
        # dims_text is "field(41) x rf_freq(81)": match inside a NAME only,
        # never in the "(41)" counts (an axis filter "4" must not find them)
        sql.append("AND EXISTS (SELECT 1 FROM json_each(files.dims_json) "
                   "WHERE json_extract(value, '$.name') LIKE ? ESCAPE '\\')")
        params.append(f"%{_like_escape(str(axis).strip())}%")
    for tag in _as_list(tags):
        sql.append("AND files.id IN (SELECT file_id FROM tags WHERE tag = ?)")
        params.append(tag.lower())
    if instrument:
        sql.append("AND files.id IN (SELECT file_id FROM instruments "
                   "WHERE slug = ? COLLATE NOCASE)")
        params.append(str(instrument).strip())
    if detector:
        d = str(detector).strip()
        sql.append("AND files.id IN (SELECT file_id FROM detectors WHERE "
                   "name = ? COLLATE NOCASE OR name LIKE ? ESCAPE '\\')")
        params += [d, f"%.{_like_escape(d)}"]
    lo, hi = _day(date_from), _day(date_to, end=True)
    if lo:
        sql.append("AND created >= ?"); params.append(lo)
    if hi:
        # an explicit datetime is inclusive; a bare date already means "<
        # next midnight"
        op = "<=" if isinstance(date_to, datetime) or (
            isinstance(date_to, str) and len(date_to.strip()) > 10) else "<"
        sql.append(f"AND created {op} ?"); params.append(hi)
    if where and where.strip():
        for term in parse_where(where):
            t_sql, t_p = _term_sql(term)
            sql.append("AND " + t_sql)
            params += t_p
    if not include_errors:
        sql.append("AND error IS NULL")
    sql.append("ORDER BY created DESC, relpath DESC")
    if limit:
        sql.append("LIMIT ?"); params.append(int(limit))

    root = Path(data_dir)
    if not index_path(root).exists():
        return []
    with closing(_connect(root)) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(" ".join(sql), params)]
    for r in rows:
        r["path"] = str(root / r["relpath"])
        r["dims"] = json.loads(r.pop("dims_json") or "[]")
        r["detectors"] = [x.strip() for x in (r.get("detectors_text") or "").split(",")
                          if x.strip()]
        r["tags"] = [x.strip() for x in (r.get("tags_text") or "").split(",") if x.strip()]
    return rows


def snapshot_of(data_dir, path) -> dict:
    """The flattened snapshot values of one indexed file: {key: value}."""
    root = Path(data_dir)
    rp = Path(path)
    try:
        rp = rp.relative_to(root)
    except ValueError:
        pass
    with closing(_connect(root)) as con:
        rows = con.execute(
            "SELECT s.key, s.num_value, s.text_value FROM snapshot_values s "
            "JOIN files f ON f.id = s.file_id WHERE f.relpath = ?",
            (rp.as_posix(),)).fetchall()
    return {k: (n if n is not None and t is not None and _num(t) == n else t)
            for k, n, t in rows}


def distinct(data_dir, column: str) -> list[str]:
    """The values a filter field has across the catalogue (for completers)."""
    allowed = {"sample", "operator", "project", "series", "structure", "setup_name"}
    if column == "setup":
        column = "setup_name"
    root = Path(data_dir)
    if column == "instrument":
        q = "SELECT DISTINCT slug FROM instruments ORDER BY slug COLLATE NOCASE"
    elif column == "tag":
        q = "SELECT DISTINCT tag FROM tags ORDER BY tag"
    elif column in allowed:
        q = (f"SELECT DISTINCT {column} FROM files WHERE {column} IS NOT NULL "
             f"ORDER BY {column} COLLATE NOCASE")
    else:
        raise ValueError(f"no such filter field: {column}")
    if not index_path(root).exists():
        return []
    with closing(_connect(root)) as con:
        return [r[0] for r in con.execute(q) if r[0]]


# ═════════════════════════════════════ CLI ════════════════════════════════════

def _default_dir() -> Path:
    try:
        from suite_common import get_setting
        got = get_setting("data_dir")
        if got:
            return Path(got)
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent / "out"


def _ascii(s) -> str:
    """Printed text is ASCII (gotcha #14): a pipe on Windows is cp1252."""
    return str(s if s is not None else "").encode("ascii", "replace").decode("ascii")


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scan_core.catalogue",
        description="Index and search the AaltoFlow data folder (catalogue.sqlite).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="bring the index up to date with the files")
    sc.add_argument("data_dir", nargs="?", help="default: the suite's data folder")
    sc.add_argument("--rebuild", action="store_true",
                    help="delete the index first and read every file again")
    se = sub.add_parser("search", help="find runs")
    se.add_argument("data_dir", nargs="?")
    for opt in ("text", "sample", "operator", "project", "series", "tags",
                "instrument", "detector", "where"):
        se.add_argument(f"--{opt}")
    se.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    se.add_argument("--to", dest="date_to", help="YYYY-MM-DD (inclusive)")
    se.add_argument("--limit", type=int)
    se.add_argument("--scan", action="store_true", help="update the index first")
    se.add_argument("--json", action="store_true", help="one JSON list on stdout")
    se.add_argument("--paths", action="store_true", help="print only the file paths")
    a = ap.parse_args(list(argv) if argv is not None else None)

    data_dir = Path(a.data_dir) if a.data_dir else _default_dir()
    if a.cmd == "scan":
        if a.rebuild:
            index_path(data_dir).unlink(missing_ok=True)
        c = scan(data_dir, progress=lambda d, n, rp: print(
            f"\r  {d}/{n}  {_ascii(rp)[:60]:<60}", end="", flush=True))
        print(f"\r{data_dir}: {c['files']} files - {c['read']} read, "
              f"{c['unchanged']} unchanged, {c['removed']} removed, "
              f"{c['errors']} unreadable ({c['seconds']:.1f} s)" + " " * 20)
        return 0
    if a.scan:
        scan(data_dir)
    try:
        rows = search(data_dir, text=a.text, sample=a.sample, operator=a.operator,
                      project=a.project, tags=a.tags, series=a.series,
                      instrument=a.instrument, detector=a.detector,
                      date_from=a.date_from, date_to=a.date_to, where=a.where,
                      limit=a.limit)
    except WhereError as exc:
        print(f"where: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=True))
    elif a.paths:
        for r in rows:
            print(_ascii(r["path"]))
    else:
        for r in rows:
            line = (f"{r['created'][:16].replace('T', ' ')}  {r['name']:<24}  "
                    f"{r.get('sample') or '-':<10}  {r.get('dims_text') or '':<28}  "
                    f"{r['relpath']}")
            if r.get("error"):
                line += f"  [unreadable: {r['error'][:60]}]"
            print(_ascii(line))
        print(f"{len(rows)} run(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
