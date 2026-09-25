"""Module discovery: one place that answers "which modules does the suite have?"

Before this, that answer was typed by hand into six files (the launcher's list,
scan-core's port table, both render scripts, the deploy script, the README), so
adding a module meant six edits and forgetting one broke something quietly.

Now there are exactly two sources, and both are data, not code:

1. `<root>/<folder>/module.toml` -- written by whoever builds the module, and
   committed with it. Identity only: key, name, description, icon, default
   ports, which scripts to run. The module's VARIABLES are deliberately not
   here: the running service reports them through `describe`, and a second
   copy would go stale.

2. `<root>/suite_local.json` -- THIS PC's choices, written by the launcher and
   NOT committed: port overrides, real-hardware flags, and the remote services
   someone added by hand. Keeping it out of git is the point: the lab PC runs
   kim on real hardware, a laptop does not.

Everything that needs the module list (launcher, scan-core, tools) calls
`discover()` and gets the same answer.
"""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = "module.toml"
LOCAL_FILE = "suite_local.json"

#: The product. A PC can add the name of the SETUP it drives ("TR-MOKE",
#: "VNA-FMR rig") with set_setup_name(); titles then lead with that, so a lab
#: with several rigs sees at a glance which one a window belongs to.
PRODUCT = "AaltoFlow"

#: Environment variables the launcher sets for what it starts. The suite was
#: called TRMOKE until 2026-09-24: readers also accept the old names, so a
#: script or shortcut somebody set up by hand keeps working.
ROOT_ENV = "AALTOFLOW_ROOT"
ENDPOINTS_ENV = "AALTOFLOW_ENDPOINTS"
LEGACY_ENV = {ROOT_ENV: "TRMOKE_ROOT", ENDPOINTS_ENV: "TRMOKE_ENDPOINTS"}


def getenv(name: str) -> str | None:
    """os.environ[name], falling back to the variable's pre-rename name."""
    return os.environ.get(name) or os.environ.get(LEGACY_ENV.get(name, ""))

#: A key is used as an id, a JSON key, a registry prefix ("kim.position_x") and
#: a folder-name stem, so it is kept boring on purpose.
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


#: What a module is FOR -- the first question when you are looking for one
#: ("I need a lock-in"). A fixed list on purpose: free text would give
#: "detector", "Detectors" and "sensor" for the same thing. key -> (label, hint).
CATEGORIES: dict[str, tuple[str, str]] = {
    "motion":      ("Motion & positioning", "stages, piezos, rotators, focus"),
    "field":       ("Magnetic field", "electromagnets, vector magnets, field control"),
    "source":      ("Signal sources", "RF / microwave generators, lasers, AWGs"),
    "detector":    ("Detectors & analyzers", "lock-ins, power meters, VNAs, spectrometers"),
    "imaging":     ("Cameras & imaging", "cameras, vision, autofocus, tracking"),
    "environment": ("Environment & safety", "temperature, cryostats, interlocks, vacuum"),
    "other":       ("Other", "anything else"),
}
DEFAULT_CATEGORY = "other"


class ManifestError(ValueError):
    """A module.toml that cannot be used, with the reason in the message."""


def default_root() -> Path:
    """The suite's root folder: where the <module>-control folders live.

    This file sits at <root>/suite-common/src/suite_common/modules.py, and the
    package is installed EDITABLE, so __file__ is the real source path and four
    levels up is the root. AALTOFLOW_ROOT overrides it (tests, a second checkout).
    """
    env = getenv(ROOT_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3]


@dataclass
class ModuleSpec:
    """One module the suite can show, start or connect to."""

    id: str                          # unique in a list: key (local) or key@host:cmd (remote)
    key: str                         # the module TYPE; equals "module" in its describe
    name: str
    description: str = ""
    order: int = 100
    dir: Path | None = None          # the project folder; None = no local copy
    icon: Path | None = None
    host: str = "localhost"
    cmd: int = 0
    pub: int = 0
    default_cmd: int = 0
    default_pub: int = 0
    service: str = ""                # script path relative to dir; "" = cannot start here
    gui: str = ""                    # script path relative to dir; "" = headless
    start_after: list = field(default_factory=list)
    category: str = DEFAULT_CATEGORY  # a CATEGORIES key: what the module is FOR
    tags: list = field(default_factory=list)   # free search words: "lock-in", "Thorlabs"
    #: set for a module known only from an ONLINE catalog (not downloaded yet);
    #: a module on disk reads its version from its pyproject.toml instead
    version: str = ""
    remote: bool = False
    real: bool = False
    #: A name safe for parameter ids and data files ("hf2", "hf2_lab2"). The id
    #: "hf2@lab2:5569" is fine for a settings file, but '@' and ':' do not
    #: belong in a recipe's parameter id or a netCDF variable name.
    slug: str = ""

    @property
    def can_start(self) -> bool:
        """Only a LOCAL module with a service script can be started from here.
        A remote service belongs to whoever runs the other PC."""
        return not self.remote and self.dir is not None and bool(self.service)

    @property
    def has_gui(self) -> bool:
        """A GUI needs the module's code on THIS PC, even for a remote service."""
        return self.dir is not None and bool(self.gui)

    @property
    def ports_overridden(self) -> bool:
        return (self.cmd, self.pub) != (self.default_cmd, self.default_pub)


@dataclass
class Discovery:
    modules: list[ModuleSpec]
    problems: list[str]              # human-readable; nothing here is fatal
    root: Path

    def get(self, id: str) -> ModuleSpec | None:
        return next((m for m in self.modules if m.id == id), None)

    def by_key(self, key: str) -> ModuleSpec | None:
        """The LOCAL module of this type (remote copies have other ids)."""
        return next((m for m in self.modules if m.key == key and not m.remote), None)


# ---------------------------------------------------------------- manifests

def parse_manifest(path: Path) -> ModuleSpec:
    """Read one module.toml. Raises ManifestError saying what is wrong."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path}: not valid TOML ({exc})") from None

    mod = data.get("module") or {}
    ports = data.get("ports") or {}
    run = data.get("run") or {}
    key = str(mod.get("key", "")).strip()
    if not _KEY.match(key):
        raise ManifestError(f"{path}: [module] key {key!r} must start with a letter "
                            f"and contain only letters, digits and _")
    name = str(mod.get("name", "")).strip()
    if not name:
        raise ManifestError(f"{path}: [module] name is missing")
    try:
        cmd = int(ports["cmd"])
        pub = int(ports.get("pub", cmd + 1))
    except (KeyError, TypeError, ValueError):
        raise ManifestError(f"{path}: [ports] cmd must be a number") from None
    for p in (cmd, pub):
        if not 1024 <= p <= 65535:
            raise ManifestError(f"{path}: port {p} outside 1024..65535")
    if cmd == pub:
        raise ManifestError(f"{path}: cmd and pub ports must differ")

    folder = path.parent
    service = str(run.get("service", "scripts/run_service.py"))
    gui = str(run.get("gui", ""))
    for label, rel in (("service", service), ("gui", gui)):
        if rel and not (folder / rel).is_file():
            raise ManifestError(f"{path}: [run] {label} script {rel!r} does not exist")
    icon_rel = str(mod.get("icon", "")).strip()
    icon = folder / icon_rel if icon_rel else None
    if icon is not None and not icon.is_file():
        icon = None                                  # a missing icon is not fatal
    after = run.get("start_after", [])
    if not isinstance(after, list):
        raise ManifestError(f"{path}: [run] start_after must be a list of keys")
    category = str(mod.get("category", DEFAULT_CATEGORY)).strip().lower()
    if category not in CATEGORIES:
        raise ManifestError(f"{path}: [module] category {category!r} is not one of "
                            + ", ".join(CATEGORIES))
    tags = mod.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ManifestError(f"{path}: [module] tags must be a list of words")

    return ModuleSpec(
        id=key, key=key, name=name,
        description=str(mod.get("description", "")).strip(),
        order=int(mod.get("order", 100)),
        dir=folder, icon=icon,
        cmd=cmd, pub=pub, default_cmd=cmd, default_pub=pub,
        service=service, gui=gui,
        start_after=[str(k) for k in after],
        category=category, tags=[t.strip() for t in tags if t.strip()],
    )


def discover_local(root: Path) -> tuple[list[ModuleSpec], list[str]]:
    """Every <root>/<folder>/module.toml, one level deep."""
    specs, problems, seen = [], [], {}
    for path in sorted(Path(root).glob(f"*/{MANIFEST}")):
        try:
            spec = parse_manifest(path)
        except ManifestError as exc:
            problems.append(str(exc))
            continue
        if spec.key in seen:
            problems.append(f"{path}: key {spec.key!r} is already used by "
                            f"{seen[spec.key]}; this one is ignored")
            continue
        seen[spec.key] = path.parent.name
        specs.append(spec)
    return specs, problems


# ------------------------------------------------------- this PC's settings

def _empty_local() -> dict:
    return {"modules": {}, "remote": []}


def load_local(root: Path | None = None) -> dict:
    """suite_local.json, or an empty structure. Never raises for a bad file:
    a broken settings file must not stop the launcher from opening."""
    path = Path(root or default_root()) / LOCAL_FILE
    try:
        # utf-8-sig: a file edited in Notepad / PowerShell starts with a BOM
        data = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError):
        return _empty_local()
    if not isinstance(data, dict):
        return _empty_local()
    data.setdefault("modules", {})
    data.setdefault("remote", [])
    if not isinstance(data["modules"], dict):
        data["modules"] = {}
    if not isinstance(data["remote"], list):
        data["remote"] = []
    return data


def save_local(data: dict, root: Path | None = None) -> None:
    """Write suite_local.json ATOMICALLY: to a temporary file, then rename.

    The launcher writes this while scan-core may be reading it. A plain write
    can be caught half-finished, which reads as invalid JSON = "no settings".
    os.replace swaps the whole file in one step.
    """
    root = Path(root or default_root())
    fd, tmp = tempfile.mkstemp(prefix=".suite_local.", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, root / LOCAL_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_ports(key: str, cmd: int | None, pub: int | None,
              root: Path | None = None) -> None:
    """Override a local module's ports on this PC; None, None = back to default."""
    data = load_local(root)
    entry = data["modules"].setdefault(key, {})
    if cmd is None:
        entry.pop("cmd", None)
        entry.pop("pub", None)
    else:
        entry["cmd"], entry["pub"] = int(cmd), int(pub if pub is not None else cmd + 1)
    save_local(data, root)


def set_real(key: str, real: bool, root: Path | None = None) -> None:
    data = load_local(root)
    data["modules"].setdefault(key, {})["real"] = bool(real)
    save_local(data, root)


def get_setting(name: str, default=None, root: Path | None = None):
    """One of this PC's plain preferences out of suite_local.json.

    Anything an application wants to remember between launches that is NOT
    about a module: which folder data goes to, and whatever comes next. They
    live under "settings" so they cannot collide with module keys, and they
    travel with the rest of this PC's choices instead of in a second file.
    """
    settings = load_local(root).get("settings")
    if not isinstance(settings, dict):
        return default
    return settings.get(name, default)


def set_setting(name: str, value, root: Path | None = None) -> None:
    """Remember a preference on this PC; value None forgets it."""
    data = load_local(root)
    settings = data.get("settings")
    if not isinstance(settings, dict):
        settings = data["settings"] = {}
    if value is None:
        settings.pop(name, None)
    else:
        settings[name] = value
    save_local(data, root)


def setup_name(root: Path | None = None) -> str:
    """The name of the setup this PC drives ("TR-MOKE"), or "" if none is set.

    Asked for by the installer and stored with this PC's other choices.
    """
    name = get_setting("setup_name", "", root)
    return name.strip() if isinstance(name, str) else ""


def set_setup_name(name: str | None, root: Path | None = None) -> None:
    """Name the setup ("" or None = just the product name)."""
    set_setting("setup_name", (name or "").strip() or None, root)


def title(what: str = "", root: Path | None = None) -> str:
    """'TR-MOKE · Mission Control', or 'AaltoFlow · Mission Control' when no
    setup is named. Every window title and banner goes through here."""
    label = setup_name(root) or PRODUCT
    return f"{label} · {what}" if what else label


def remote_id(key: str, host: str, cmd: int) -> str:
    return f"{key}@{host}:{int(cmd)}"


def add_remote(host: str, cmd: int, pub: int, key: str, name: str = "",
               description: str = "", root: Path | None = None) -> str:
    """Remember a service running on another PC. Returns its id.

    `key` is the module type the service reported in `describe`, which is how
    the launcher finds the icon, description and GUI of the same module here.
    """
    host = str(host).strip()
    if not host:
        raise ValueError("host is empty")
    if not _KEY.match(key or ""):
        raise ValueError(f"module key {key!r} is not valid")
    rid = remote_id(key, host, cmd)
    data = load_local(root)
    if any(remote_id(r.get("key", ""), r.get("host", ""), r.get("cmd", 0)) == rid
           for r in data["remote"]):
        raise ValueError(f"{rid} is already in the list")
    data["remote"].append({"host": host, "cmd": int(cmd), "pub": int(pub),
                           "key": key, "name": name, "description": description})
    save_local(data, root)
    return rid


def remove_remote(id: str, root: Path | None = None) -> bool:
    """Forget a remote service. Returns False if it was not in the list."""
    data = load_local(root)
    keep = [r for r in data["remote"]
            if remote_id(r.get("key", ""), r.get("host", ""), r.get("cmd", 0)) != id]
    if len(keep) == len(data["remote"]):
        return False
    data["remote"] = keep
    save_local(data, root)
    return True


# ------------------------------------------------------------- the answer

def discover(root: Path | None = None) -> Discovery:
    """Local modules (with this PC's overrides) + remote services, ordered."""
    root = Path(root or default_root())
    local, problems = discover_local(root)
    settings = load_local(root)
    by_key = {m.key: m for m in local}

    for m in local:
        over = settings["modules"].get(m.key) or {}
        try:
            if "cmd" in over:
                m.cmd = int(over["cmd"])
                m.pub = int(over.get("pub", m.cmd + 1))
        except (TypeError, ValueError):
            problems.append(f"{LOCAL_FILE}: bad port override for {m.key}; using defaults")
            m.cmd, m.pub = m.default_cmd, m.default_pub
        m.real = bool(over.get("real", False))

    remote = []
    for r in settings["remote"]:
        try:
            key = str(r["key"])
            host = str(r["host"])
            cmd = int(r["cmd"])
            pub = int(r.get("pub", cmd + 1))
        except (KeyError, TypeError, ValueError):
            problems.append(f"{LOCAL_FILE}: a remote entry is incomplete and was skipped: {r!r}")
            continue
        twin = by_key.get(key)                   # same module type, installed here
        remote.append(ModuleSpec(
            id=remote_id(key, host, cmd), key=key,
            name=str(r.get("name") or (twin.name if twin else key)),
            description=str(r.get("description") or (twin.description if twin else "")),
            order=twin.order if twin else 1000,
            dir=twin.dir if twin else None,
            icon=twin.icon if twin else None,
            host=host, cmd=cmd, pub=pub, default_cmd=cmd, default_pub=pub,
            service="",                           # never started from this PC
            gui=twin.gui if twin else "",
            remote=True,
        ))

    modules = sorted(local, key=lambda m: (m.order, m.name.lower()))
    modules += sorted(remote, key=lambda m: (m.order, m.name.lower(), m.id))
    _assign_slugs(modules)
    problems += port_conflicts(modules)
    return Discovery(modules=modules, problems=problems, root=root)


def _assign_slugs(modules: list[ModuleSpec]) -> None:
    """key for local modules; key_host for remote ones (key_host_port if that
    is still ambiguous). Unique within the list."""
    taken = {m.key for m in modules if not m.remote}
    for m in modules:
        if not m.remote:
            m.slug = m.key
            continue
        host = re.sub(r"[^A-Za-z0-9]+", "_", m.host).strip("_") or "remote"
        slug = f"{m.key}_{host}"
        if slug in taken:
            slug = f"{slug}_{m.cmd}"
        m.slug = slug
        taken.add(slug)


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}


def port_conflicts(modules: list[ModuleSpec]) -> list[str]:
    """Two modules on the same machine cannot bind the same port."""
    used: dict[tuple[str, int], str] = {}
    out = []
    for m in modules:
        host = "this PC" if m.host.lower() in _LOCAL_HOSTS else m.host.lower()
        for port in (m.cmd, m.pub):
            slot = (host, port)
            if slot in used and used[slot] != m.id:
                out.append(f"port {port} on {host} is used by both "
                           f"{used[slot]} and {m.id}")
            else:
                used[slot] = m.id
    return out


def start_order(modules: list[ModuleSpec]) -> list[ModuleSpec]:
    """Order a set of modules so every `start_after` dependency comes first.

    Only dependencies INSIDE the set matter: camera lists kim, piezo and zpiezo,
    but starting just camera + kim must not wait for a piezo that is not coming.
    A cycle (a mistake in two manifests) falls back to list order, reported by
    `check_modules`, rather than hanging the launcher.
    """
    pending = list(modules)
    keys = {m.key for m in pending}
    done: list[ModuleSpec] = []
    placed: set[str] = set()
    while pending:
        progressed = False
        for m in list(pending):
            if all(k in placed or k not in keys for k in m.start_after):
                done.append(m)
                placed.add(m.key)
                pending.remove(m)
                progressed = True
        if not progressed:                       # a cycle: keep the given order
            done += pending
            break
    return done


# -------------------------------------------------------- talking to them

def probe(host: str, port: int, timeout: float = 0.2) -> bool:
    """True if something accepts a TCP connection on host:port.

    Cheap, and answers "is a service up?" without speaking ZeroMQ. It cannot
    tell WHICH service is listening -- `describe` does that.
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def service_args(m: ModuleSpec) -> list[str]:
    """Arguments for `run_service.py`: the contract every module accepts."""
    args = ["--cmd-port", str(m.cmd), "--pub-port", str(m.pub)]
    if m.real:
        args.append("--real")
    return args


def gui_args(m: ModuleSpec, connect: bool) -> list[str]:
    """Arguments for `run_gui.py`. Without `connect` the GUI runs its own sim."""
    if not connect:
        return []
    host = "localhost" if m.host.lower() in _LOCAL_HOSTS else m.host
    return ["--connect", host, "--cmd-port", str(m.cmd), "--pub-port", str(m.pub)]


def endpoints_json(modules: list[ModuleSpec]) -> str:
    """Where every module listens, for the AALTOFLOW_ENDPOINTS environment variable.

    Local modules are listed under their key, so a module that talks to another
    (camera -> kim) can find it by type. Remote ones only under their full id:
    a remote kim must not silently replace the local one.
    """
    table = {}
    for m in modules:
        name = m.id if m.remote else m.key
        table[name] = [m.host, m.cmd, m.pub]
    return json.dumps(table)
