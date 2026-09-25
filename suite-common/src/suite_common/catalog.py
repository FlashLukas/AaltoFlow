"""
catalog.py -- what modules exist, and how one gets onto a PC.

Three jobs, all without Qt so they are tested on their own and shared by every
front end (Mission Control's "Add module..." today, an online catalog later):

  CATALOG   build_catalog(): every module described for someone SHOPPING for
            one -- what it is for (category), search words (tags), ports,
            version. tools/make_catalog.py writes it to <root>/catalog.json.

  SOURCES   ModuleSource(path): the modules inside a folder or a .zip (a
            "module pack" made by tools/pack_module.py, or any zip of module
            folders). A pack may carry a `wheels/` folder -- every Python
            package the module needs -- so it installs with NO internet.

  INSTALL   plan_install() says what installing each module would do (new /
            update / conflict, and whether its ports clash); install() does it;
            env_steps() lists the commands that build the module's .venv,
            online (uv sync) or offline (from the wheelhouse).

The rule that makes updating safe is the installer's (installer/gen_components.py):
a module's settings (*.ini), calibrations (*calibration*.json) and its
Calibrations folder are tuned on the rig, so an update NEVER overwrites them.

Standard library only, like the rest of suite_common.
"""

from __future__ import annotations

import fnmatch
import json
import os
import hashlib
import shutil
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .modules import (CATEGORIES, MANIFEST, PRODUCT, ManifestError, ModuleSpec,
                      discover, discover_local, parse_manifest, set_ports)

CATALOG_FILE = "catalog.json"
CATALOG_FORMAT = "aaltoflow-catalog/1"
PACK_FILE = "pack.json"
PACK_FORMAT = "aaltoflow-module-pack/1"
#: Where offline packages live on an installed PC: one shared folder, so a
#: later rebuild of the same module is offline too.
WHEELHOUSE = "wheelhouse"

#: Never copied: environments, caches, build products, version control.
_SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".git", ".mypy_cache",
              ".ruff_cache", "build", "dist"}
_SKIP_SUFFIXES = (".pyc", ".pyo")


def _skipped_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.endswith(".egg-info")


def is_lab_data(rel: str) -> bool:
    """True for the files a rig TUNES and an update must therefore not replace.

    Same rule as the installer: <module>/*.ini, <module>/*calibration*.json,
    and anything under <module>/Calibrations/. `rel` is relative to the module
    folder, with / separators.
    """
    parts = rel.split("/")
    if parts[0] == "Calibrations":
        return True
    if len(parts) == 1:
        return (fnmatch.fnmatch(parts[0].lower(), "*.ini")
                or fnmatch.fnmatch(parts[0].lower(), "*calibration*.json"))
    return False


def module_version(folder: Path) -> str:
    """[project] version from the module's pyproject.toml, or ''."""
    try:
        with open(folder / "pyproject.toml", "rb") as fh:
            return str(tomllib.load(fh).get("project", {}).get("version", ""))
    except (OSError, tomllib.TOMLDecodeError):
        return ""


def project_extras(folder: Path) -> list[str]:
    """The extras an environment build must name.

    `uv sync` REMOVES every extra it is not told about (docs/DEVELOPER_NOTES.md gotcha
    #29), so the gui extra -- and `real`, where a module keeps its hardware
    driver there -- must always be passed. Same list as postinstall.ps1.
    """
    try:
        with open(folder / "pyproject.toml", "rb") as fh:
            opt = tomllib.load(fh).get("project", {}).get("optional-dependencies", {})
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [x for x in ("gui", "real") if x in opt]


# ------------------------------------------------------------------ catalog

def catalog_entry(m: ModuleSpec) -> dict:
    label, _hint = CATEGORIES[m.category]
    return {
        "key": m.key, "name": m.name, "description": m.description,
        "category": m.category, "category_label": label, "tags": list(m.tags),
        "folder": m.dir.name if m.dir else "",
        "version": module_version(m.dir) if m.dir else "",
        "ports": {"cmd": m.default_cmd, "pub": m.default_pub},
        "gui": bool(m.gui), "start_after": list(m.start_after),
    }


def build_catalog(modules: list[ModuleSpec]) -> dict:
    """The catalog document: categories first, then the modules in order.

    Deliberately WITHOUT a timestamp, so regenerating an unchanged suite gives
    a byte-identical file and check_modules.py can tell whether it is stale.
    """
    mods = sorted((m for m in modules if not m.remote and m.dir is not None),
                  key=lambda m: (m.order, m.key))
    return {
        "format": CATALOG_FORMAT, "product": PRODUCT,
        "categories": {k: {"label": v[0], "hint": v[1]} for k, v in CATEGORIES.items()},
        "modules": [catalog_entry(m) for m in mods],
    }


def catalog_text(modules: list[ModuleSpec]) -> str:
    return json.dumps(build_catalog(modules), indent=2, ensure_ascii=False) + "\n"


def search(modules: list[ModuleSpec], text: str = "", category: str = "") -> list[ModuleSpec]:
    """Filter by category and by words found in name / description / tags / key."""
    words = [w for w in text.lower().split() if w]
    out = []
    for m in modules:
        if category and m.category != category:
            continue
        hay = " ".join([m.key, m.name, m.description, *m.tags]).lower()
        if all(w in hay for w in words):
            out.append(m)
    return out


# ------------------------------------------------------------------ sources

def _find_manifests(base: Path) -> list[Path]:
    """module.toml at the top, else one level down, else two (a zip of a
    folder of modules)."""
    if (base / MANIFEST).is_file():
        return [base / MANIFEST]
    for pattern in (f"*/{MANIFEST}", f"*/*/{MANIFEST}"):
        found = sorted(p for p in base.glob(pattern)
                       if not any(_skipped_dir(x) for x in p.relative_to(base).parts))
        if found:
            return found
    return []


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract, refusing any member that would land outside `dest`
    ("zip slip": a name like ../../Windows/x.dll or an absolute path)."""
    dest = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if dest != target and dest not in target.parents:
            raise ValueError(f"unsafe path in the zip: {info.filename!r}")
    zf.extractall(dest)


class ModuleSource:
    """The modules found in a folder or a .zip, ready to be planned/installed.

        with ModuleSource(path) as src:
            src.modules, src.problems, src.pack, src.wheels

    A zip is unpacked into a temporary folder that disappears on close, so
    install BEFORE closing (install() copies what it needs, wheels included).
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._tmp: tempfile.TemporaryDirectory | None = None
        if self.path.is_file() and zipfile.is_zipfile(self.path):
            self._tmp = tempfile.TemporaryDirectory(prefix="aaltoflow_pack_")
            with zipfile.ZipFile(self.path) as zf:
                _safe_extract(zf, Path(self._tmp.name))
            self.base = Path(self._tmp.name)
        elif self.path.is_dir():
            self.base = self.path
        else:
            raise ValueError(f"{self.path} is neither a folder nor a .zip")

        self.pack: dict | None = None
        pack_file = next(iter(sorted(self.base.glob(PACK_FILE))), None) \
            or next(iter(sorted(self.base.glob(f"*/{PACK_FILE}"))), None)
        if pack_file is not None:
            try:
                self.pack = json.loads(pack_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.pack = None
        wheels = (pack_file.parent if pack_file else self.base) / "wheels"
        self.wheels: Path | None = wheels if wheels.is_dir() else None

        self.modules: list[ModuleSpec] = []
        self.problems: list[str] = []
        seen: dict[str, str] = {}
        for mf in _find_manifests(self.base):
            try:
                spec = parse_manifest(mf)
            except ManifestError as exc:
                self.problems.append(str(exc).replace(str(self.base), self.path.name))
                continue
            if spec.key in seen:
                self.problems.append(f"{mf.parent.name}: key {spec.key!r} appears twice "
                                     f"(also {seen[spec.key]}); ignored")
                continue
            seen[spec.key] = mf.parent.name
            self.modules.append(spec)
        if not self.modules and not self.problems:
            self.problems.append(f"no {MANIFEST} found in {self.path.name}")

    def offline_for(self, key: str) -> bool:
        """Does this source carry the packages to build `key` without internet?"""
        return self.wheels is not None and (self.wheels / requirements_name(key)).is_file()

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def requirements_name(key: str) -> str:
    return f"{key}-requirements.txt"


# ------------------------------------------------------------------ install

@dataclass
class InstallPlan:
    spec: ModuleSpec                   # as found in the SOURCE
    target: Path                       # <root>/<same folder name>
    action: str                        # "new" | "update" | "conflict" | "same"
    reason: str = ""
    #: ports to give it on THIS PC when its defaults are taken (None = keep)
    ports: tuple[int, int] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def installable(self) -> bool:
        return self.action in ("new", "update")


def _next_free(used: set[int]) -> tuple[int, int]:
    """The first pair 5555 + 2n with both ports free (the suite's port scheme)."""
    n = 0
    while True:
        cmd = 5555 + 2 * n
        if cmd not in used and cmd + 1 not in used:
            return cmd, cmd + 1
        n += 1


def plan_install(specs: list[ModuleSpec], root: Path) -> list[InstallPlan]:
    """What installing each module into `root` would do. Changes nothing."""
    root = Path(root)
    installed, _ = discover_local(root)
    by_key = {m.key: m for m in installed}
    # ports in use on THIS PC: every local module's EFFECTIVE ports (with this
    # PC's overrides) -- and remote ones do not matter, they listen elsewhere
    used = {p for m in discover(root).modules if not m.remote for p in (m.cmd, m.pub)}
    plans = []
    for spec in specs:
        target = root / spec.dir.name
        plan = InstallPlan(spec=spec, target=target, action="new")
        have = by_key.get(spec.key)
        if spec.dir.resolve() == target.resolve():
            plan.action, plan.reason = "same", "this is the installed copy itself"
        elif have is not None and have.dir.name != spec.dir.name:
            plan.action = "conflict"
            plan.reason = (f"key {spec.key!r} is already used by the folder "
                           f"{have.dir.name}")
        elif target.exists():
            try:
                there = parse_manifest(target / MANIFEST) if (target / MANIFEST).is_file() else None
            except ManifestError:
                there = None
            if there is None:
                plan.action = "conflict"
                plan.reason = f"a folder {target.name} exists and is not a working module"
            elif there.key != spec.key:
                plan.action = "conflict"
                plan.reason = f"the folder {target.name} holds the module {there.key!r}"
            else:
                plan.action = "update"
                old, new = module_version(target), spec.version or module_version(spec.dir)
                plan.reason = (f"replaces version {old or '?'} with {new or '?'}; "
                               "its settings and calibrations are kept")
        if plan.action == "new":
            if spec.default_cmd in used or spec.default_pub in used:
                plan.ports = _next_free(used)
                plan.notes.append(f"its ports {spec.default_cmd}/{spec.default_pub} are "
                                  f"taken here; it will use {plan.ports[0]}/{plan.ports[1]}")
            got = plan.ports or (spec.default_cmd, spec.default_pub)
            used.update(got)                     # the next new one must not take them
            plan.reason = plan.reason or "new on this PC"
        plans.append(plan)
    return plans


def _files(src: Path):
    """Every file to copy, walking with the skipped folders PRUNED -- a .venv
    holds tens of thousands of files that must not even be visited."""
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if not _skipped_dir(d))
        for f in sorted(filenames):
            if not f.endswith(_SKIP_SUFFIXES):
                yield Path(dirpath) / f


def install(plan: InstallPlan, root: Path, wheels: Path | None = None) -> list[str]:
    """Copy one planned module into `root`. Returns what happened, line by line.

    An UPDATE copies the new code over the old but keeps every lab-data file
    that already exists (see is_lab_data). Files the new version no longer has
    are left where they are -- deleting inside a rig's folder is not something
    to do implicitly.
    """
    if not plan.installable:
        raise ValueError(f"{plan.spec.key}: cannot install ({plan.action}: {plan.reason})")
    root = Path(root)
    src, dst = plan.spec.dir, plan.target
    log = []
    kept = copied = 0
    for path in _files(src):
        rel_parts = path.relative_to(src).parts
        rel = "/".join(rel_parts)
        out = dst.joinpath(*rel_parts)
        if out.exists() and is_lab_data(rel):
            kept += 1
            log.append(f"kept your {rel}")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        copied += 1
    log.insert(0, f"{plan.spec.key}: {copied} files copied into {dst.name}"
                  + (f", {kept} of your files kept" if kept else ""))
    if plan.ports:
        set_ports(plan.spec.key, plan.ports[0], plan.ports[1], root)
        log.append(f"{plan.spec.key}: ports {plan.ports[0]}/{plan.ports[1]} on this PC")
    if wheels is not None and wheels.is_dir():
        house = root / WHEELHOUSE
        house.mkdir(exist_ok=True)
        n = 0
        for w in wheels.iterdir():
            if w.is_file() and (not (house / w.name).exists()
                                or w.name.endswith("-requirements.txt")):
                shutil.copy2(w, house / w.name)
                n += 1
        log.append(f"{n} package files added to {WHEELHOUSE}\\ (offline builds)")
    return log


# --------------------------------------------------------------- environments

def find_uv(root: Path | None = None) -> str | None:
    """uv, even when it is not on PATH: Setup ships one in <root>/uv/, and the
    standalone installer puts it in ~/.local/bin, which a shell
    or a shortcut may not have on PATH (the case on the lab PC)."""
    found = shutil.which("uv")
    if found:
        return found
    cands = []
    if root is not None:
        cands += [Path(root) / "uv" / "uv.exe", Path(root) / "uv" / "uv"]
    cands += [Path.home() / ".local" / "bin" / "uv.exe",
              Path.home() / ".local" / "bin" / "uv",
              Path.home() / ".cargo" / "bin" / "uv.exe"]
    return next((str(c) for c in cands if c.exists()), None)


@dataclass
class Step:
    label: str
    argv: list[str]
    cwd: Path


def env_steps(folder: Path, root: Path, uv: str, offline: bool) -> list[Step]:
    """The commands that build `folder`'s .venv (run them in order).

    ONLINE is what the installer does: `uv sync` with every declared extra,
    from the module's committed uv.lock -- exactly the tested versions.

    OFFLINE uses <root>/wheelhouse, filled by a module pack: a .venv, then the
    pinned requirements (exported from that same uv.lock when the pack was
    made), then the module itself, editable, like uv sync would. No index is
    consulted at all (--no-index), so a missing package is a clear error
    instead of a silent attempt to reach PyPI from a lab subnet that blocks it.
    """
    folder, root = Path(folder), Path(root)
    key = parse_manifest(folder / MANIFEST).key
    if not offline:
        extras = [a for x in project_extras(folder) for a in ("--extra", x)]
        return [Step(f"{key}: uv sync (online)", [uv, "sync", *extras], folder)]
    house = root / WHEELHOUSE
    req = house / requirements_name(key)
    if not req.is_file():
        raise FileNotFoundError(f"{req} is missing: this module was not installed "
                                "from a pack with packages (--wheels)")
    common = ["--offline", "--no-index", "--find-links", str(house)]
    return [
        Step(f"{key}: create .venv", [uv, "venv", ".venv", "--allow-existing",
                                      "--offline"], folder),
        Step(f"{key}: install packages (offline)",
             [uv, "pip", "install", "--python", ".venv", *common, "-r", str(req)], folder),
        Step(f"{key}: install the module itself",
             [uv, "pip", "install", "--python", ".venv", *common,
              "--no-deps", "--no-build-isolation", "-e", "."], folder),
    ]


def write_pack_json(dest: Path, modules: list[ModuleSpec], commit: str,
                    python: str | None) -> None:
    """The pack's label: what is inside and what it was built for."""
    doc = {
        "format": PACK_FORMAT, "product": PRODUCT, "commit": commit,
        "created": datetime.now().isoformat(timespec="seconds"),
        "offline": python is not None, "python": python,
        "modules": [catalog_entry(m) for m in modules],
    }
    (dest / PACK_FILE).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")


# ------------------------------------------------------------------ online

#: Where the suite's modules are published: the catalog of the newest GitHub
#: release. "releases/latest/download/<file>" always points at the newest
#: release, so this address never changes. A lab can mirror a release on a
#: share and point the setting `catalog_url` there instead (file:// or http://).
DEFAULT_CATALOG_URL = ("https://github.com/FlashLukas/AaltoFlow/releases/latest/"
                       "download/catalog.json")


class CatalogError(RuntimeError):
    """The online catalog or a download failed; the message says what to do."""


def _file_url(path: Path) -> str:
    return Path(path).resolve().as_uri()


def _normalize_url(url: str) -> str:
    r"""Accept a plain Windows path to a mirror (T:\mirror\catalog.json, or a
    \\server\share path) too, not only a URL."""
    url = url.strip()
    if (len(url) > 2 and url[1] == ":") or url.startswith("\\\\"):
        return _file_url(Path(url))
    return url


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": f"{PRODUCT}-module-installer"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CatalogError(
                f"not found: {url}\nIs the repository public, and has a module release "
                "been published (tools/release_modules.py)?") from None
        raise CatalogError(f"{url}: HTTP {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CatalogError(f"cannot reach {url}: {reason}\nNo internet on this PC? "
                           "Use a module pack (.zip) instead.") from None


def fetch_catalog(url: str = DEFAULT_CATALOG_URL, timeout: float = 20) -> dict:
    """Download and check a release catalog. Adds '_url' (where it came from),
    which download links are resolved against."""
    url = _normalize_url(url)
    with _open(url, timeout) as resp:
        raw = resp.read()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise CatalogError(f"{url} is not a catalog (not JSON)") from None
    if not isinstance(doc, dict) or doc.get("format") != CATALOG_FORMAT:
        raise CatalogError(f"{url} is not an {PRODUCT} catalog "
                           f"(format {doc.get('format') if isinstance(doc, dict) else '?'})")
    doc["_url"] = url
    return doc


def asset_url(catalog: dict, file: str) -> str:
    """A download's address, RELATIVE to the catalog -- so a mirrored folder
    works without editing the catalog."""
    return urllib.parse.urljoin(catalog["_url"], file)


def spec_from_entry(entry: dict) -> ModuleSpec:
    """A ModuleSpec for a module known only from a catalog (not downloaded).
    Enough to show it, search it and plan its install; `dir` is a placeholder
    that exists nowhere, so it can never be mistaken for an installed copy."""
    ports = entry.get("ports", {})
    return ModuleSpec(
        id=entry["key"], key=entry["key"], name=entry.get("name", entry["key"]),
        description=entry.get("description", ""), order=int(entry.get("order", 100)),
        dir=Path("<online>") / entry.get("folder", entry["key"] + "-control"),
        cmd=int(ports.get("cmd", 0)), pub=int(ports.get("pub", 0)),
        default_cmd=int(ports.get("cmd", 0)), default_pub=int(ports.get("pub", 0)),
        category=entry.get("category", "other") if entry.get("category") in CATEGORIES
        else "other",
        tags=list(entry.get("tags", [])), start_after=list(entry.get("start_after", [])),
        gui="gui" if entry.get("gui") else "", version=str(entry.get("version", "")),
    )


def download(url: str, dest: Path, sha256: str | None = None, size: int | None = None,
             progress=None, timeout: float = 30, cancelled=None) -> Path:
    """Stream `url` to `dest`, verifying the SHA-256 the catalog promised.

    Written to `dest.part` and renamed only when the hash matches: a broken or
    tampered download never looks like a finished one. `progress(done, total)`
    is called per chunk; `cancelled()` returning True stops it.
    """
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    done = 0
    try:
        with _open(url, timeout) as resp, open(part, "wb") as fh:
            total = size or int(resp.headers.get("Content-Length") or 0) or None
            while True:
                if cancelled is not None and cancelled():
                    raise CatalogError("download cancelled")
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
        if sha256 and h.hexdigest() != sha256.lower():
            raise CatalogError(f"{dest.name}: checksum mismatch -- the download is "
                               "damaged or not the published file; nothing was installed")
        os.replace(part, dest)
    finally:
        if part.exists():
            part.unlink()
    return dest


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def release_catalog(modules: list[ModuleSpec], assets: dict, commit: str,
                    tag: str, python: str | None) -> dict:
    """The catalog published WITH a release: the plain catalog plus, per module,
    its downloads -- {kind: {file, size, sha256}}, kind "code" (small; packages
    come from PyPI when the environment is built) or "offline" (carries them).
    `assets` = {key: {kind: Path}}; files are named relative to the catalog."""
    doc = build_catalog(modules)
    doc.update({"release": tag, "commit": commit, "python": python,
                "created": datetime.now().isoformat(timespec="seconds")})
    icons = {m.key: m.icon for m in modules}
    for entry in doc["modules"]:
        # the icon travels INSIDE the catalog (a few hundred bytes each), so the
        # wizard shows each module's picture before anything is downloaded
        icon = icons.get(entry["key"])
        if icon is not None:
            try:
                entry["icon_svg"] = icon.read_text(encoding="utf-8")
            except OSError:
                pass
        entry["downloads"] = {
            kind: {"file": p.name, "size": p.stat().st_size, "sha256": file_digest(p)}
            for kind, p in sorted(assets.get(entry["key"], {}).items())}
    doc["modules"] = [e for e in doc["modules"] if e["downloads"]]
    return doc
