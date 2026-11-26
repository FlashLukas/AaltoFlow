"""settings_bundle.py -- this PC's settings in ONE zip file, and back.

Why this exists. Every setting the rig learns lives as a plain file inside the
install folder: a module's tuned `.ini`, a calibration, the launcher's
profiles. Reinstalling into the SAME folder keeps them (the installer never
overwrites lab data), but a new PC, or an install into another folder, starts
from zero -- and an ordinary user has no backup tool. So Mission Control can
write everything into one .zip ("Export settings...") and read it back
("Import settings...").

WHAT is a setting is decided by RULE, not by a list, so a module added next
year is covered without anyone editing this file. The rule is the installer's
own (installer/gen_components.py and catalog.is_lab_data -- one function, so
"what the installer protects" and "what the backup saves" cannot drift apart):

    <module>/*.ini                  settings tuned on the rig
    <module>/*calibration*.json     e.g. kim's px_calibration.json
    <module>/Calibrations/**        clMag's calibration folder
    scan-core/lab_predictors/*.py   a lab's own window predictors (its own
                                    physics, see scan_core/predictors.py);
                                    Python CODE, so only in scan-core, and
                                    the import summary says so

applied to every discovered module folder and to the suite's own folders
(mission-control, scan-core, suite-common), plus three named files:

    suite_local.json                THIS PC's ports, real flags, remote hosts,
                                    setup name, data folder
    mission-control/profiles.json   the launcher's profile chips
    scan-core/suite_layouts.json    the control panel's layouts

Paths in the zip are relative to the suite root, so a module's files are
stored as `modules/<category>/<folder>/...` (the layout since 2026-09-27).
A bundle made BEFORE that move stores them as `<folder>/...`; import still
accepts those and puts each file into the folder the module has now.

The zip also carries a small manifest, `aaltoflow-settings.json`: format,
product, when it was made and the file list. Deliberately NO computer name and
NO user name: a bundle may be mailed around or attached to a bug report.

IMPORT is careful because it overwrites files:
  * read_bundle() only PLANS: which files are new, which would be replaced,
    which are identical, and which are SKIPPED (and why). Nothing is written.
  * A member is skipped when its path escapes the suite folder ("zip slip":
    `../../Windows/x.dll`, an absolute path, a drive letter), when it is not a
    settings file by the rule above (a bundle must never be able to drop code
    into a module), or when it belongs to a module that is not installed here.
  * apply_import() first zips every file it is about to replace into
    `.suite_cache/settings-backup-<time>.zip` -- itself a settings bundle, so
    importing it undoes the import -- then writes each file ATOMICALLY
    (temporary file + os.replace), so a crash never leaves half a calibration.

Standard library only, like the rest of suite_common.
"""

from __future__ import annotations

import hashlib
import json
import re
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from .catalog import _skipped_dir, is_lab_data
from .modules import (LOCAL_FILE, MODULES_DIR, PRODUCT, default_root, discover_local,
                      rel_to_root)

BUNDLE_FORMAT = "aaltoflow-settings/1"
MANIFEST_NAME = "aaltoflow-settings.json"
#: Where import backups go: the launcher's local-state folder (gitignored).
BACKUP_DIR = ".suite_cache"

#: Suite folders that are not modules (no module.toml) but can still hold lab
#: data -- the installer protects the same three.
SUITE_FOLDERS = ("suite-common", "mission-control", "scan-core")

#: A lab's own window predictors (scan_core/predictors.py LAB_DIR): the one
#: place a bundle may carry Python code -- only here, only *.py at its top.
LAB_PREDICTORS = "scan-core/lab_predictors"

#: Settings files that the folder rule would not catch, by exact path.
NAMED_FILES = (LOCAL_FILE, "mission-control/profiles.json", "scan-core/suite_layouts.json")

# Security review 2026-11-26: a bundle is DATA, but three things in it could
# have made a module run a program from it -- so import checks them:
#: the file types a Calibrations/ folder may receive (data, never a program:
#: a .dll there plus a dll_path in an .ini would have been loaded by a service)
CALIBRATION_SUFFIXES = (".json", ".csv", ".txt", ".ini", ".dat", ".npy", ".npz",
                        ".png", ".tif", ".tiff", ".cal", ".yaml", ".yml", ".xml",
                        ".h5", ".nc")
#: .ini keys that name a program library a service loads (pm16 dll_path, the
#: camera's thorlabs_dll_dir, a VISA library...). Import never takes their
#: value from a bundle: it keeps this PC's line, or leaves the key out.
LIBRARY_KEY = re.compile(r"(dll|sdk|librar)", re.I)
#: suite_local.json keys whose import deserves to be named in the dialog
LOCAL_RISKY = ("real", "instances", "remote", "catalog_url", "eln_url",
               "eln_verify_tls", "eln_auto_upload", "data_dir")


# ------------------------------------------------------------ what to save

def _settings_folders(root: Path) -> list[str]:
    """Folders (relative to root, / separators) whose lab data belongs in a
    bundle: "modules/motion/kim-control", ..., "scan-core"."""
    specs, _problems = discover_local(root)
    names = {rel_to_root(root, m.dir) for m in specs if m.dir is not None}
    names |= {f for f in SUITE_FOLDERS if (root / f).is_dir()}
    return sorted(names)


def _split(rel: str, folders) -> tuple[str, str] | None:
    """(folder, rest) when `rel` lies inside one of `folders`, else None."""
    for f in sorted(folders, key=len, reverse=True):     # longest match first
        if rel.startswith(f + "/"):
            return f, rel[len(f) + 1:]
    return None


def is_settings_path(rel: str, folders: list[str] | set[str]) -> bool:
    """True if `rel` (relative to the suite root, / separators) is a setting.

    `folders` = the folders a setting may live in (see _settings_folders).
    """
    if rel in NAMED_FILES:
        return True
    hit = _split(rel, folders)
    if hit is None or not hit[1] or not is_lab_data(hit[1]):
        return False
    if hit[1].startswith("lab_predictors/"):
        # code that scan-core imports: never into any other folder
        return f"{hit[0]}/lab_predictors" == LAB_PREDICTORS
    return True


def _old_layout_map(folders) -> dict[str, str]:
    """Leaf folder name -> today's folder, for bundles made before modules/
    existed ("kim-control" -> "modules/motion/kim-control")."""
    out = {}
    for f in folders:
        leaf = f.rsplit("/", 1)[-1]
        if f != leaf:
            out.setdefault(leaf, f)
    return out


def collect(root: Path | None = None) -> list[str]:
    """Every settings file that exists under `root`, as sorted relative paths
    with / separators (the form a zip uses on every OS)."""
    root = Path(root or default_root())
    found: set[str] = set()
    for folder in _settings_folders(root):
        base = root / folder
        # Top level of the folder: *.ini and *calibration*.json.
        for f in base.iterdir():
            if f.is_file() and is_lab_data(f.name):
                found.add(f"{folder}/{f.name}")
        # The Calibrations folder, recursively (skipping caches, just in case).
        cal = base / "Calibrations"
        if cal.is_dir():
            for f in cal.rglob("*"):
                rel_parts = f.relative_to(base).parts
                if f.is_file() and not any(_skipped_dir(p) for p in rel_parts):
                    found.add(folder + "/" + "/".join(rel_parts))
    lab = root / LAB_PREDICTORS
    if lab.is_dir():
        for f in lab.iterdir():
            rel = f"{LAB_PREDICTORS}/{f.name}"
            if f.is_file() and is_settings_path(rel, _settings_folders(root)):
                found.add(rel)
    for rel in NAMED_FILES:
        if (root / rel).is_file():
            found.add(rel)
    return sorted(found)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_zip(dest: Path, files: dict[str, bytes], note: str = "") -> None:
    """Write a bundle atomically: temp file next to `dest`, then os.replace.
    A crash halfway leaves the previous file (or nothing), never a broken zip."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": BUNDLE_FORMAT,
        "product": PRODUCT,
        # Local time, to the second: "when did I make this backup?" is the
        # question it answers. Nothing about WHERE (no PC or user name).
        "created": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "files": [{"path": rel, "size": len(data), "sha256": _sha256(data)}
                  for rel, data in sorted(files.items())],
    }
    fd, tmp = tempfile.mkstemp(prefix=".bundle.", suffix=".tmp", dir=dest.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")
            for rel, data in sorted(files.items()):
                zf.writestr(rel, data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_bundle(dest: str | Path, root: Path | None = None) -> list[str]:
    """Write every settings file into the zip `dest`. Returns the paths saved."""
    root = Path(root or default_root())
    files = {rel: (root / rel).read_bytes() for rel in collect(root)}
    _write_zip(Path(dest), files, note="exported settings")
    return sorted(files)


# ------------------------------------------------------------ reading back

NEW, OVERWRITE, SAME, SKIP = "new", "overwrite", "unchanged", "skipped"


@dataclass
class BundleEntry:
    path: str                  # relative to the suite root, / separators
    action: str                # NEW / OVERWRITE / SAME / SKIP
    reason: str = ""           # why it is skipped (or where an old-layout file came from)
    data: bytes = field(default=b"", repr=False)


@dataclass
class ImportPlan:
    source: Path
    manifest: dict
    entries: list[BundleEntry]

    def _with(self, action: str) -> list[BundleEntry]:
        return [e for e in self.entries if e.action == action]

    @property
    def new(self) -> list[BundleEntry]:
        return self._with(NEW)

    @property
    def overwrite(self) -> list[BundleEntry]:
        return self._with(OVERWRITE)

    @property
    def unchanged(self) -> list[BundleEntry]:
        return self._with(SAME)

    @property
    def skipped(self) -> list[BundleEntry]:
        return self._with(SKIP)

    @property
    def to_write(self) -> list[BundleEntry]:
        return self.new + self.overwrite

    @property
    def touches_code(self) -> bool:
        """True when a lab predictor (Python code scan-core imports) would be
        written -- the summary says so: import such a bundle only from a
        source you trust."""
        return any(e.path.startswith(LAB_PREDICTORS + "/") for e in self.to_write)

    @property
    def code_entries(self) -> list[BundleEntry]:
        """The lab predictors (Python code) this import would write."""
        return [e for e in self.to_write if e.path.startswith(LAB_PREDICTORS + "/")]

    def without_code(self) -> "ImportPlan":
        """The same plan with the Python files left out (marked SKIP)."""
        out = []
        for e in self.entries:
            if e in self.code_entries:
                e = BundleEntry(e.path, SKIP, "Python code, not imported")
            out.append(e)
        return ImportPlan(self.source, self.manifest, out)

    def local_warnings(self) -> list[str]:
        """What importing suite_local.json would switch on or point elsewhere
        on THIS PC: real hardware, extra service arguments, remote hosts, the
        module catalog, the ELN upload, the data folder. Shown in the dialog
        itself, not only in the details."""
        e = next((e for e in self.to_write if e.path == LOCAL_FILE), None)
        if e is None:
            return []
        try:
            new = json.loads(e.data.decode("utf-8-sig"))
        except ValueError:
            return ["suite_local.json in the bundle is not valid JSON"]
        out = []
        mods = new.get("modules") if isinstance(new, dict) else None
        for key, m in (mods or {}).items() if isinstance(mods, dict) else ():
            if not isinstance(m, dict):
                continue
            if m.get("real"):
                out.append(f"{key}: REAL hardware on")
            for inst in m.get("instances") or []:
                if isinstance(inst, dict) and (inst.get("args") or inst.get("config")):
                    out.append(f"{key}: an instance with extra arguments / config file "
                               f"({inst.get('args') or ''} {inst.get('config') or ''})".rstrip())
        if isinstance(new, dict) and new.get("remote"):
            hosts = sorted({str(r.get("host")) for r in new["remote"] if isinstance(r, dict)})
            out.append("remote services on " + ", ".join(hosts))
        st = new.get("settings") if isinstance(new, dict) else None
        if isinstance(st, dict):
            for k in ("catalog_url", "eln_url", "eln_verify_tls", "eln_auto_upload", "data_dir"):
                if k in st:
                    out.append(f"{k} = {st[k]!r}")
        return out

    @property
    def touches_local_settings(self) -> bool:
        """True when suite_local.json would change -- worth a warning, because
        it holds THIS PC's ports, real-hardware flags and remote hosts."""
        return any(e.path == LOCAL_FILE for e in self.to_write)


def _clean_member(name: str) -> str | None:
    """A zip member name as a safe relative path, or None if it is unsafe.

    Refuses what could land outside the suite folder: absolute paths, drive
    letters ("C:"), and any ".." part. Backslashes count as separators too --
    a zip made by a careless Windows tool may use them, and Windows would
    follow them.
    """
    parts = name.replace("\\", "/").split("/")
    if name.startswith(("/", "\\")) or any(p in ("..",) or ":" in p for p in parts):
        return None
    parts = [p for p in parts if p not in ("", ".")]
    return "/".join(parts) if parts else None


def _keep_library_keys(data: bytes, current: bytes) -> tuple[bytes, list[str]]:
    """`data` (an .ini from a bundle) with every line that names a program
    library (LIBRARY_KEY: dll_path, thorlabs_dll_dir, visa_library...) taken
    from `current` (this PC's file) instead, or removed when this PC's file has
    no such line. Returns the new bytes and the keys that differed.

    Why: a module loads that library when it starts with --real. From a
    bundle -- mailed around, attached to a bug report -- the path could name a
    DLL anywhere, a network share included. It is a setting of THIS PC, set
    by hand, like the COM port of a real instrument would be."""
    line_re = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*[=:]\s*(.*?)\s*$")

    def parse(b: bytes) -> tuple[list[str], dict]:
        lines = b.decode("utf-8-sig", errors="replace").splitlines()
        sec, found = "", {}
        for ln in lines:
            s = ln.strip()
            if s.startswith("[") and s.endswith("]"):
                sec = s[1:-1].strip().lower()
                continue
            m = line_re.match(ln)
            if m and LIBRARY_KEY.search(m.group(1)):
                found[(sec, m.group(1).lower())] = ln
        return lines, found

    lines, _ = parse(data)
    _, mine = parse(current)
    out, sec, changed = [], "", []
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            sec = s[1:-1].strip().lower()
            out.append(ln)
            continue
        m = line_re.match(ln)
        if m and LIBRARY_KEY.search(m.group(1)):
            here = mine.get((sec, m.group(1).lower()))
            here_val = line_re.match(here).group(2) if here else ""
            if m.group(2) != here_val:
                changed.append(m.group(1))
            if here is not None:
                out.append(here)
            continue
        out.append(ln)
    if not changed:
        return data, []
    nl = "\r\n" if b"\r\n" in data else "\n"
    return (nl.join(out) + nl).encode("utf-8"), changed


def read_bundle(path: str | Path, root: Path | None = None) -> ImportPlan:
    """Open a bundle and say what importing it would do. Writes nothing.

    Raises ValueError when the file is not a settings bundle at all.
    """
    root = Path(root or default_root()).resolve()
    path = Path(path)
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"{path.name} is not a zip file ({exc})") from None
    with zf:
        try:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        except KeyError:
            raise ValueError(f"{path.name} is not an {PRODUCT} settings bundle "
                             f"(no {MANIFEST_NAME} inside)") from None
        except ValueError as exc:
            raise ValueError(f"{path.name}: {MANIFEST_NAME} is not valid JSON ({exc})") from None
        fmt = str(manifest.get("format", "")) if isinstance(manifest, dict) else ""
        if not fmt.startswith("aaltoflow-settings/"):
            raise ValueError(f"{path.name} is not an {PRODUCT} settings bundle "
                             f"(format {fmt!r})")
        if fmt != BUNDLE_FORMAT:
            # A NEWER format may mean files this version does not understand.
            raise ValueError(f"{path.name} was made by a newer version "
                             f"({fmt}); update {PRODUCT} first")

        folders = set(_settings_folders(root))
        old_layout = _old_layout_map(folders)
        entries: list[BundleEntry] = []
        for info in zf.infolist():
            if info.is_dir() or info.filename == MANIFEST_NAME:
                continue
            rel = _clean_member(info.filename)
            if rel is None:
                entries.append(BundleEntry(info.filename, SKIP,
                                           "unsafe path (outside the suite folder)"))
                continue
            note = ""
            head, _, rest = rel.partition("/")
            if (rel not in NAMED_FILES and rest and _split(rel, folders) is None
                    and head in old_layout):
                # A bundle from before the move to modules/: "kim-control/kim.ini"
                # goes to wherever kim-control is now.
                note = f"was {rel} in the bundle (older layout)"
                rel = f"{old_layout[head]}/{rest}"
            # Belt and braces: the resolved target must really be under root.
            target = (root / rel).resolve()
            if root not in target.parents:
                entries.append(BundleEntry(rel, SKIP, "unsafe path (outside the suite folder)"))
                continue
            head = rel.partition("/")[0]
            if rel not in NAMED_FILES and "/" in rel and _split(rel, folders) is None:
                what = "/".join(rel.split("/")[:3]) if head == MODULES_DIR else head
                entries.append(BundleEntry(rel, SKIP, f"{what} is not installed here"))
                continue
            if not is_settings_path(rel, folders):
                entries.append(BundleEntry(rel, SKIP, "not a settings file"))
                continue
            if rel in NAMED_FILES and "/" in rel and not (root / head).is_dir():
                entries.append(BundleEntry(rel, SKIP, f"{head} is not installed here"))
                continue
            data = zf.read(info)
            # A Calibrations/ folder takes data files only (see
            # CALIBRATION_SUFFIXES): a program dropped there could be loaded
            # through an .ini's dll_path.
            if "/Calibrations/" in f"/{rel}" and not rel.lower().endswith(CALIBRATION_SUFFIXES):
                entries.append(BundleEntry(rel, SKIP, "not a data file (a program?)"))
                continue
            if rel.lower().endswith(".ini"):
                current = target.read_bytes() if target.is_file() else b""
                data, kept = _keep_library_keys(data, current)
                if kept:
                    note = "; ".join(x for x in (note, "library setting(s) kept as on "
                                                 "this PC: " + ", ".join(kept)) if x)
            if target.is_file():
                action = SAME if target.read_bytes() == data else OVERWRITE
            else:
                action = NEW
            entries.append(BundleEntry(rel, action, reason=note, data=data))
    entries.sort(key=lambda e: e.path)
    return ImportPlan(source=path, manifest=manifest, entries=entries)


def summary(plan: ImportPlan) -> str:
    """Plain-text description of an import, for a confirmation dialog. ASCII."""
    m = plan.manifest
    lines = [f"Bundle: {plan.source.name}",
             f"Made: {m.get('created', '?')}   ({len(plan.entries)} file(s))", ""]
    for title, group in (("Will be REPLACED (a backup is kept)", plan.overwrite),
                         ("New", plan.new),
                         ("Unchanged (identical here)", plan.unchanged),
                         ("Skipped", plan.skipped)):
        if not group:
            continue
        lines.append(f"{title}: {len(group)}")
        for e in group:
            lines.append(f"    {e.path}" + (f"  -- {e.reason}" if e.reason else ""))
        lines.append("")
    if plan.touches_local_settings:
        lines.append(f"NOTE: {LOCAL_FILE} holds THIS PC's port overrides, real-hardware "
                     "flags and remote hosts. Importing it replaces them with the ones "
                     "of the PC the bundle came from.")
    if plan.touches_code:
        lines.append(f"NOTE: files in {LAB_PREDICTORS} are Python code that the "
                     "measurement suite runs (a lab's own window predictors). "
                     "Import them only from a bundle you trust.")
    return "\n".join(lines).rstrip() + "\n"


def apply_import(plan: ImportPlan, root: Path | None = None) -> Path | None:
    """Write the plan's new and changed files. Returns the backup zip made of
    the files it replaced (None if nothing was replaced).

    The backup is a settings bundle, so importing it puts the replaced files
    back. It does not remove files the import CREATED; those are listed in
    the plan and can be deleted by hand.
    """
    root = Path(root or default_root())
    backup = None
    if plan.overwrite:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = root / BACKUP_DIR / f"settings-backup-{stamp}.zip"
        n = 1
        while backup.exists():                       # two imports in one second
            backup = root / BACKUP_DIR / f"settings-backup-{stamp}-{n}.zip"
            n += 1
        _write_zip(backup, {e.path: (root / e.path).read_bytes() for e in plan.overwrite},
                   note=f"backup made before importing {plan.source.name}")
    for e in plan.to_write:
        target = root / e.path
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".import.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(e.data)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return backup


def default_bundle_name(now: datetime | None = None) -> str:
    """'aaltoflow-settings-2026-09-25.zip' -- a date, and nothing about the PC."""
    return f"aaltoflow-settings-{(now or datetime.now()):%Y-%m-%d}.zip"
