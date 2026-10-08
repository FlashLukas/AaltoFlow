"""file_modules.py -- which instrument modules a scan file was measured with.

Lukas (2026-10-08): "can we start modules according to the modules used by a
data file?"  Mission Control answers that, but it cannot open a .nc itself:
its environment has no xarray / h5py, and it must never import scan-core or an
instrument package. So it runs THIS file in scan-core's own environment and
reads one line of JSON from stdout:

    python -m scan_core.file_modules FILE.nc

    {"file": "...", "source": "snapshot",
     "modules": [{"slug": "kim", "key": "kim", "real": false,
                  "idn_model": "SIM KIM101 3-axis piezo-inertia stage",
                  "source": "snapshot", "in_recipe": true}, ...]}

and, when the file cannot be read, the same shape with an "error" text and an
empty list (exit code 1), so the caller never has to parse a traceback.

Where the answer comes from
---------------------------
* A file written since 2026-10-04 carries an instrument SNAPSHOT (snapshot.py):
  one attribute per connected instrument, holding the module key, its config,
  its `info` and its status. That is the full list, including instruments the
  recipe never touched (the snapshot records every connected one, because the
  setting that spoils a map is usually on an instrument nobody thought of).
* An older file only has the recipe (attribute `recipe_json`). Its parameter
  ids are "<slug>.<id>" ("kim.position_x", "hf2_lab2.r1"), so the slugs can be
  read off them. Those files cannot say real or simulated: "real" is null.

Real or simulated?
------------------
Nothing in the snapshot is a dedicated "this was the simulator" flag for every
module, so it is read from what the modules already report:
  1. an explicit boolean `simulated` in the instrument's info or status (vna,
     scope, ppms, tc200, mag2d ... have one) -- trusted first;
  2. otherwise the instrument's idn: EVERY simulator in the suite says so in
     its idn ("SIMULATED", "(simulated)", "SIM KIM101 ...", "SimCamera"), and
     a real driver returns what the instrument itself answers;
  3. no idn (clMag reports none; or the PC switched idn storage off with the
     suite setting snapshot_include_idn) -> null, "unknown".

PRIVACY: an idn usually carries the instrument's SERIAL NUMBER. Only the
manufacturer + model part is printed (`idn_model`); serials never leave this
function. (The serial stays in the data file itself, which is the lab's.)

Pure Python apart from opening the file (xarray, lazily), and printed text is
ASCII (gotcha #14: Mission Control reads it through a pipe).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .snapshot import read_snapshot

#: The dataset attribute holding the recipe (engine.py writes it).
RECIPE_ATTR = "recipe_json"

#: A slug: what scan-core prefixes a module's parameter ids with -- the module
#: key for a local module ("kim"), key_host for one on another PC ("hf2_lab2").
_SLUG = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
#: A prefixed parameter id: "<slug>.<id>". Anything else (a sim registry's
#: bare "field", a file name "a.csv", a number "1.5") is not a module reference.
_PREFIXED = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_.\[\]]*)$")

#: Recipe keys whose STRING value (or list of strings) names a parameter or an
#: action. Walking only these keeps comments, file paths and formulas out.
_ID_VALUE_KEYS = frozenset({"param", "speed_param", "readback", "move", "action",
                            "detector", "detectors", "x_param", "y_param",
                            "param_x", "param_y", "target", "focus_param"})
#: Recipe keys whose dict KEYS are parameter ids ({set: {kim.position_x: 3}}).
_ID_KEY_KEYS = frozenset({"fixed", "set"})

#: How a simulator announces itself in its idn (see the module docstring).
#: "simulat" anywhere (SIMULATED, simulated, simulator), the word SIM / sim on
#: its own ("SIM KIM101", "ELL14 (sim)", "SIM0001"), or a name starting with
#: "Sim" + capital ("SimCamera", "SimZ").
_SIM_WORD = re.compile(r"(^|[^A-Za-z])(SIM|sim)([^A-Za-z]|$)|^Sim[A-Z]")
_SIM_TEXT = re.compile(r"simulat", re.IGNORECASE)

#: Pieces of an idn that identify ONE unit, removed from idn_model.
_SERIAL_TEXT = re.compile(r"\b(S/?N|serial(\s*(no\.?|number))?)\s*[:#]?\s*\S+.*$",
                          re.IGNORECASE)
_FIRMWARE_TEXT = re.compile(r"\b(fw|firmware|ver(sion)?)\b.*$", re.IGNORECASE)
#: A word that is probably a serial or an address, dropped from free text:
#: a run of 5+ digits (model numbers have at most 4: PM16-121, SMB100A, 4886,
#: N5222A), a VISA resource ("USB0::0x0699::...::<serial>::INSTR"), or a
#: quoted word (zpiezo's real idn quotes the KCube serial).
_SERIALISH = re.compile(r"\d{5,}|::|^['\"].*['\"]$")
#: Words after which a free-text idn says WHERE it is plugged in ("Newport
#: AG-UC2 on COM5", "d-Drive @ COM3"): this PC's business, not the model.
_WHERE_WORDS = frozenset({"on", "@", "at"})
#: A firmware version on its own ("AG-UC2 v2.0"): not part of the model.
_VERSION_WORD = re.compile(r"^v\d[\d.]*$")
MAX_MODEL = 80


# ---------------------------------------------------------------- the recipe

def _recipe_of(attrs: dict) -> dict:
    """The recipe dict stored in the file, {} if absent or unreadable."""
    text = attrs.get(RECIPE_ATTR)
    if not text:
        return {}
    try:
        d = json.loads(text)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _ids_in(node, out: set, key: str = "") -> None:
    """Collect every parameter / action id in a recipe (walked recursively)."""
    if isinstance(node, dict):
        for k, v in node.items():
            k = str(k)
            if k in _ID_KEY_KEYS and isinstance(v, dict):
                out.update(str(x) for x in v)
            _ids_in(v, out, k)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _ids_in(v, out, key)
    elif isinstance(node, str) and key in _ID_VALUE_KEYS:
        out.add(node)


def recipe_slugs(recipe: dict) -> list[str]:
    """The module slugs a recipe's parameter ids name, in first-seen order
    of a sorted walk (stable for tests and for the dialog)."""
    ids: set = set()
    _ids_in(recipe, ids)
    slugs = []
    for pid in sorted(ids):
        m = _PREFIXED.match(pid)
        if m and m.group(1) not in slugs:
            slugs.append(m.group(1))
    return slugs


def key_from_slug(slug: str) -> str:
    """Best guess of a module key from a slug alone (old files): a remote slug
    is key_host, and no module key in the suite contains '_'. Mission Control
    refines this with the keys it actually knows."""
    return slug.split("_", 1)[0]


# ---------------------------------------------------------------- real or sim

def _idn_of(entry: dict) -> str:
    for part in ("info", "status"):
        block = entry.get(part)
        if isinstance(block, dict):
            v = block.get("idn")
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def is_sim_idn(idn: str) -> bool:
    """True when an idn text is a simulator's (see _SIM_TEXT / _SIM_WORD)."""
    return bool(_SIM_TEXT.search(idn) or _SIM_WORD.search(idn))


def real_from_entry(entry: dict):
    """True (real instrument) / False (simulator) / None (the file cannot tell)."""
    for part in ("info", "status"):
        block = entry.get(part)
        if isinstance(block, dict) and isinstance(block.get("simulated"), bool):
            return not block["simulated"]
    idn = _idn_of(entry)
    if idn:
        return not is_sim_idn(idn)
    return None


def model_from_idn(idn: str) -> str:
    """Manufacturer + model from an idn, WITHOUT the serial number.

    A SCPI *IDN? answer is "maker,model,serial,firmware": keep the first two
    fields. Free text ("Thorlabs PM16-121 S/N 1234 fw 1.5") is cut at the
    serial / firmware marker.
    """
    idn = (idn or "").strip()
    if not idn:
        return ""
    parts = [p.strip() for p in idn.split(",")]
    if len(parts) >= 2:
        text = " ".join(p for p in parts[:2] if p)
    else:
        text = idn
    text = _FIRMWARE_TEXT.sub("", _SERIAL_TEXT.sub("", text))
    words = []
    for w in text.split():
        if w.lower() in _WHERE_WORDS:
            break
        if not (_SERIALISH.search(w) or _VERSION_WORD.match(w)):
            words.append(w)
    return " ".join(words).strip(" ,;-")[:MAX_MODEL]


def _model_of(entry: dict) -> str:
    info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
    model = info.get("model")
    if isinstance(model, str) and model.strip():
        return model_from_idn(model)          # same serial guard
    return model_from_idn(_idn_of(entry))


# ---------------------------------------------------------------- the answer

def _attrs_of(source) -> dict:
    if isinstance(source, dict):
        return source
    if hasattr(source, "attrs"):
        return dict(source.attrs)
    import xarray as xr                       # only here: opening a file
    with xr.open_dataset(source) as ds:
        return dict(ds.attrs)


def modules_used(source) -> dict:
    """{"file", "source", "modules": [...]} for a .nc path, a Dataset or an
    attrs dict. Never raises: a problem is returned as "error"."""
    name = str(source) if isinstance(source, (str, Path)) else ""
    out = {"file": name, "source": "none", "modules": []}
    try:
        attrs = _attrs_of(source)
    except Exception as exc:
        out["error"] = f"cannot read the file: {type(exc).__name__}: {exc}"
        return out

    in_recipe = set(recipe_slugs(_recipe_of(attrs)))
    snap = read_snapshot(attrs)
    rows = []
    if snap:
        out["source"] = "snapshot"
        for slug, entry in snap.items():
            key = entry.get("module") if isinstance(entry.get("module"), str) else ""
            rows.append({"slug": slug, "key": key or key_from_slug(slug),
                         "real": real_from_entry(entry), "idn_model": _model_of(entry),
                         "source": "snapshot", "in_recipe": slug in in_recipe})
    # a recipe slug the snapshot lacks (an instrument that failed to answer
    # at the start, or an old file without any snapshot) still counts
    seen = {r["slug"] for r in rows}
    for slug in sorted(in_recipe - seen):
        rows.append({"slug": slug, "key": key_from_slug(slug), "real": None,
                     "idn_model": "", "source": "recipe", "in_recipe": True})
        if out["source"] == "none":
            out["source"] = "recipe"
    out["modules"] = rows
    if not rows:
        out["error"] = ("the file names no instrument module (made with the "
                        "simulated registry, or not a scan-core file)")
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m scan_core.file_modules",
        description="Print, as JSON, the instrument modules a scan file used.")
    ap.add_argument("file", help="a .nc written by scan-core")
    args = ap.parse_args(argv)
    result = modules_used(args.file)
    result["file"] = str(args.file)
    # ensure_ascii: the reader is a pipe (gotcha #14)
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 1 if result.get("error") and not result["modules"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
