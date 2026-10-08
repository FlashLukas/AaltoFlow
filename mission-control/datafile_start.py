"""Mission Control's "Start for a data file...": bring up the modules a scan used.

Lukas (2026-10-08): "can we start modules according to the modules used by a
data file?"

Every scan file written by scan-core records which instruments were connected
(the instrument SNAPSHOT, scan-core/scan_core/snapshot.py) and the recipe,
whose parameter ids name their module ("kim.position_x"). So a file is, among
other things, a list of modules -- a profile that was never saved.

Three pieces, kept apart so each can be tested on its own:

1. ``run_helper`` -- the launcher cannot read a .nc itself: its environment
   has no xarray / h5py, and it must never import scan-core or an instrument
   package (a version of one would then be tied to the version of the other).
   So it runs ``python -m scan_core.file_modules FILE`` in scan-core's OWN
   environment and reads the one line of JSON it prints. MainWindow calls it
   from a background thread with a timeout: the first start of an interpreter
   can take seconds, and a frozen launcher looks like a crash (gotcha #21).

2. ``plan_rows`` -- pure Python, no Qt: the helper's answer + the modules this
   PC discovered -> one row per module the file used: which card it is, what
   that card is doing now, whether to tick it, and whether the file was
   measured in the other real/sim mode than the card is set to.

3. ``DataFileDialog`` (datafile_dialog.py, the only part that needs Qt) --
   shows the rows; the operator ticks, then picks Start, Start + open GUIs,
   or Save as profile. The dialog only RETURNS the choice; MainWindow does the
   starting through the same path a profile chip uses.

The real/sim flag is SHOWN, never changed: switching a card to real hardware
moves real instruments, and that is the operator's decision, made on the card.

This file imports no Qt, so the mapping is tested without a screen.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: scan-core's folder in the suite root, and the module the helper runs.
SCAN_CORE_DIR = "scan-core"
HELPER_MODULE = "scan_core.file_modules"
#: Generous: the first `uv run` on a PC may still have to build the environment.
HELPER_TIMEOUT_S = 90.0

#: How the dialog writes the file's real/sim answer.
REAL_TEXT = {True: "real", False: "sim", None: "?"}

#: state_kind values of a row (the dialog colours by them)
RUNNING, UP_ELSEWHERE, STOPPED, MISSING, REMOTE, OTHER_PC = (
    "running", "up", "stopped", "missing", "remote", "other_pc")


# ───────────────────────────── 1. the helper ─────────────────────────────────

def helper_command(root: Path, find_uv=None):
    """(program, args) that run the helper in scan-core's environment, or None.

    The same places Mission Control looks for any project's interpreter: the
    project's own .venv, else %LOCALAPPDATA%\\uv-venvs\\scan-core (where
    dev.ps1 puts environments of a tree inside OneDrive), else `uv run`, which
    finds or builds the environment itself (slower the first time).
    """
    project = Path(root) / SCAN_CORE_DIR
    if not project.is_dir():              # its environment alone cannot run it
        return None
    cands = [project / ".venv" / "Scripts" / "python.exe",
             project / ".venv" / "bin" / "python"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "uv-venvs" / SCAN_CORE_DIR / "Scripts" / "python.exe")
    for py in cands:
        if py.exists():
            return str(py), ["-m", HELPER_MODULE]
    uv = find_uv() if find_uv else None
    if uv:
        return uv, ["run", "--project", str(project), "python", "-m", HELPER_MODULE]
    return None


def _failed(path, why: str) -> dict:
    return {"file": str(path), "modules": [], "error": why}


def run_helper(root: Path, path, find_uv=None, timeout_s: float = HELPER_TIMEOUT_S,
               runner=subprocess.run) -> dict:
    """The helper's JSON for one file. Never raises: every failure (no
    scan-core here, a timeout, garbage on stdout) comes back as "error".

    BLOCKS for as long as the helper runs: call it from a thread.
    `runner` is subprocess.run (tests pass a fake).
    """
    cmd = helper_command(root, find_uv)
    if cmd is None:
        return _failed(path, f"scan-core is not installed here ({Path(root) / SCAN_CORE_DIR}) "
                             f"and uv was not found: nothing can read a .nc file")
    prog, args = cmd
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        r = runner([prog, *args, str(path)], cwd=str(Path(root) / SCAN_CORE_DIR),
                   capture_output=True, timeout=timeout_s, env=env,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return _failed(path, f"reading the file took longer than {timeout_s:.0f} s")
    except OSError as exc:
        return _failed(path, f"could not run scan-core's Python ({prog}): {exc}")
    out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
    # the answer is the LAST line that is a JSON object: anything a library
    # prints on the way (a deprecation notice) comes before it
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                doc = json.loads(line)
            except ValueError:
                break
            if isinstance(doc, dict) and isinstance(doc.get("modules"), list):
                doc.setdefault("file", str(path))
                return doc
            break
    err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
    tail = " / ".join(ln.strip() for ln in err.strip().splitlines()[-2:]) or f"exit code {r.returncode}"
    return _failed(path, f"scan-core's helper gave no answer ({tail})")


# ───────────────────────────── 2. the mapping ────────────────────────────────

@dataclass
class FileRow:
    """One module the file used, as this PC sees it."""
    slug: str                    # the name in the file ("kim", "hf2_lab2")
    key: str                     # the module type ("kim", "hf2")
    name: str                    # what the dialog calls it (card name, else the key)
    card_id: str | None          # the matching card, None = no card here
    state: str                   # "running here", "stopped", ...
    state_kind: str              # RUNNING / UP_ELSEWHERE / STOPPED / MISSING / REMOTE / OTHER_PC
    can_start: bool              # a local card with a service script
    has_gui: bool
    ticked: bool                 # the default tick
    tick_enabled: bool           # False = nothing here to start or include
    file_real: bool | None       # what the FILE was measured with (None = unknown)
    card_real: bool | None       # the card's "real" box now (None = no such box)
    warning: str = ""            # a real/sim mismatch, said plainly
    note: str = ""               # what to do about a row that cannot be ticked
    idn_model: str = ""
    source: str = ""             # "snapshot" | "recipe"
    in_recipe: bool = False      # scanned / recorded by the recipe (not only connected)


def resolve_key(slug: str, key: str, local_keys) -> str:
    """The module key for a slug. The helper's key is right for a snapshot; for
    an old (recipe-only) file it guessed from the slug, so prefer a key this PC
    knows that the slug is, or starts with + "_" (the longest such key wins)."""
    local_keys = set(local_keys)
    if slug in local_keys:
        return slug
    if key in local_keys:
        return key
    best = ""
    for k in local_keys:
        if slug.startswith(k + "_") and len(k) > len(best):
            best = k
    return best or key or slug


def mismatch_text(file_real, card_real) -> str:
    if file_real is True and card_real is False:
        return ("the file was measured with the REAL instrument; "
                "this card is set to simulation")
    if file_real is False and card_real is True:
        return ("the file was measured with the SIMULATOR; "
                "this card is set to real hardware")
    return ""


def plan_rows(result: dict, modules, up=None, owned=None) -> list[FileRow]:
    """The helper's answer -> one FileRow per module, in the file's order.

    `modules` are this PC's discovered ModuleSpecs; `up` = {card id: port
    answers}; `owned` = ids of cards whose service THIS launcher started.
    Pure: no Qt, no network, nothing started.
    """
    up = up or {}
    owned = set(owned or ())
    by_slug = {m.slug: m for m in modules}
    local = {m.key: m for m in modules if not m.remote}
    remotes_by_key: dict[str, list] = {}
    for m in modules:
        if m.remote:
            remotes_by_key.setdefault(m.key, []).append(m)

    rows: list[FileRow] = []
    seen: set[str] = set()
    for entry in result.get("modules") or []:
        slug = str(entry.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        key = resolve_key(slug, str(entry.get("key") or ""), local)
        file_real = entry.get("real") if isinstance(entry.get("real"), bool) else None
        common = dict(slug=slug, key=key, file_real=file_real,
                      idn_model=str(entry.get("idn_model") or ""),
                      source=str(entry.get("source") or ""),
                      in_recipe=bool(entry.get("in_recipe")))
        spec = by_slug.get(slug)

        if spec is not None and not spec.remote:
            card_real = bool(spec.real) if spec.is_instrument else None
            is_up = bool(up.get(spec.id))
            mine = spec.id in owned
            if mine and is_up:
                state, kind = "running here", RUNNING
            elif mine:
                state, kind = "starting...", RUNNING
            elif is_up:
                state, kind = "up (started elsewhere)", UP_ELSEWHERE
            else:
                state, kind = "stopped", STOPPED
            rows.append(FileRow(
                **common, name=spec.name, card_id=spec.id, state=state, state_kind=kind,
                can_start=spec.can_start, has_gui=spec.has_gui,
                # tick by default only what still needs starting
                ticked=kind == STOPPED and spec.can_start,
                tick_enabled=True, card_real=card_real,
                warning=mismatch_text(file_real, card_real)))
            continue

        if spec is not None:                       # a remote card with this slug
            state = "remote, reachable" if up.get(spec.id) else "remote, not answering"
            rows.append(FileRow(
                **common, name=spec.name, card_id=spec.id, state=state, state_kind=REMOTE,
                can_start=False, has_gui=spec.has_gui, ticked=False, tick_enabled=True,
                card_real=None,
                note=f"runs on {spec.host}: started there, not from here"))
            continue

        twin = local.get(key)
        if slug != key:
            # the file reached this module on ANOTHER PC (slug key_host), and
            # there is no remote card for it here
            note = "on another PC (add it with Add remote...)"
            if twin is not None:
                note += (f"; if THIS is that PC, start the local {twin.name} "
                         f"card instead")
            rows.append(FileRow(
                **common, name=twin.name if twin else key, card_id=None,
                state="on another PC", state_kind=OTHER_PC, can_start=False,
                has_gui=False, ticked=False, tick_enabled=False, card_real=None, note=note))
            continue

        # a local module of the file's PC that this PC does not have
        note = "not installed on this PC (Add module... installs it)"
        others = remotes_by_key.get(key) or []
        if others:
            note += "; a remote card of this type exists: " + ", ".join(m.id for m in others)
        rows.append(FileRow(
            **common, name=key, card_id=None, state="NOT INSTALLED here",
            state_kind=MISSING, can_start=False, has_gui=False, ticked=False,
            tick_enabled=False, card_real=None, note=note))
    return rows


def source_text(result: dict) -> str:
    src = result.get("source")
    if src == "snapshot":
        return ("from the instrument snapshot in the file (every instrument that was "
                "connected, real or simulated as recorded)")
    if src == "recipe":
        return ("from the scan definition only (an older file without an instrument "
                "snapshot: it cannot say real or simulated)")
    return ""
