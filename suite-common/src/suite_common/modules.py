"""Module discovery: one place that answers "which modules does the suite have?"

Before this, that answer was typed by hand into six files (the launcher's list,
scan-core's port table, both render scripts, the deploy script, the README), so
adding a module meant six edits and forgetting one broke something quietly.

Now there are exactly two sources, and both are data, not code:

1. `<root>/modules/<category>/<folder>/module.toml` -- written by whoever
   builds the module, and committed with it (see `manifest_paths` for where
   they are looked for). Identity only: key, name, description, icon, default
   ports, which scripts to run. The module's VARIABLES are deliberately not
   here: the running service reports them through `describe`, and a second
   copy would go stale.

2. `<root>/suite_local.json` -- THIS PC's choices, written by the launcher and
   NOT committed: port overrides, real-hardware flags, the remote services
   someone added by hand, and the extra INSTANCES of a local module (a second
   copy of one module run side by side, see add_instance). Keeping it out of
   git is the point: the lab PC runs kim on real hardware, a laptop does not.

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
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path

from .fileio import replace_retry

MANIFEST = "module.toml"
LOCAL_FILE = "suite_local.json"
#: The folder that holds the instrument modules, sorted by what they are FOR:
#: <root>/modules/<category>/<key>-control. The suite's own projects
#: (mission-control, scan-core, suite-common) stay directly in <root>.
MODULES_DIR = "modules"

#: The suite's own projects. They live directly in <root>, never under
#: modules/. One of them may still carry a module.toml: scan-core does since
#: 2026-10-05 -- its SCAN SERVER (scan_core/scan_server.py) is a service with
#: the suite's wire contract, so Mission Control shows it as a card and another
#: PC can "Add remote..." it. Such a module is a SUITE PROJECT: it stays where
#: it is (tools/migrate_layout.py must never move scan-core), it is installed
#: with its project (not as a module of its own) and it is never packed or
#: published as a module (see ModuleSpec.suite_project).
SUITE_PROJECTS = ("mission-control", "scan-core", "suite-common")

#: Module keys that are NOT instruments: services that DRIVE the instruments.
#: A scan engine following the launcher must not connect to them as if they
#: were one (the scan server would otherwise appear as parameters "scanserver.*"
#: on the Scan tab), see ModuleSpec.is_instrument.
COORDINATOR_KEYS = frozenset({"scanserver"})

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
#: An instance name ("dwf", "b", "lab2") becomes part of a slug ("scope_dwf")
#: and so of every parameter id the scan engine registers ("scope_dwf.ch1"):
#: letters, digits and _ only, for the same reason as a key.
_INSTANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*$")
#: Separates key and instance name in an instance's id ("scope#dwf"). Like the
#: '@' of a remote id it can never appear in a key, so ids cannot collide.
INSTANCE_SEP = "#"
#: Flags an instance may NOT carry in its extra arguments, because the
#: launcher passes them itself (service_args): its ports, --real, the bind
#: host, and --config (the instance's own `config` field is the place for it).
RESERVED_ARGS = ("--cmd-port", "--pub-port", "--real", "--host", "--config")


#: What a module is FOR -- the first question when you are looking for one
#: ("I need a lock-in"). A fixed list on purpose: free text would give
#: "detector", "Detectors" and "sensor" for the same thing. key -> (label, hint).
#: what kind of address a module's [hardware] address_arg wants: a VISA
#: resource, a COM port, an IP / host, or "device" -- an id the module's own
#: vendor library understands verbatim (a Kinesis serial, "Dev1", "dev1234"),
#: which only the module's probe or the USB list can offer
ADDRESS_BUSES = ("visa", "serial", "ip", "device")

CATEGORIES: dict[str, tuple[str, str]] = {
    "motion":      ("Motion & positioning", "stages, piezos, rotators, focus"),
    "field":       ("Magnetic field", "electromagnets, vector magnets, field control"),
    "source":      ("Signal sources", "RF / microwave generators, lasers, AWGs"),
    "detector":    ("Detectors & analyzers", "lock-ins, power meters, VNAs, spectrometers"),
    "imaging":     ("Cameras & imaging", "cameras, vision, autofocus, tracking"),
    "environment": ("Environment & safety", "temperature, cryostats, interlocks, vacuum"),
    # 2026-09-29, for the NI USB-6001: a general DAQ's purpose depends on what is
    # wired to it (inputs, outputs, digital lines), so it fits none of the above
    "io":          ("General I/O", "DAQ cards, analog / digital lines, relays, triggers"),
    # 2026-10-05, for the scan server: not an instrument but a service that
    # drives the instruments (scan engines, schedulers)
    "coordination": ("Coordination", "scan servers and other services that drive the modules"),
    "other":       ("Other", "anything else"),
}
DEFAULT_CATEGORY = "other"


class ManifestError(ValueError):
    """A module.toml that cannot be used, with the reason in the message."""


def default_root() -> Path:
    """The suite's root folder: the one that holds modules/, scan-core, ...

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

    id: str                          # unique in a list: key (local), key#name (an
                                     # instance) or key@host:cmd (remote)
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
    #: How the module's service is told WHICH instrument to open (module.toml
    #: [hardware]): the flag its run_service.py takes ("--visa", "--port"),
    #: and what kind of address that flag wants -- "visa" (a VISA resource
    #: string), "serial" (a COM port) or "ip" (a host or IP address). Empty
    #: = the module takes no address from the launcher. Mission Control's
    #: "Instruments on this PC" uses both to offer a found address to the
    #: modules it fits.
    address_arg: str = ""
    address_bus: str = ""
    #: [hardware] probe: a script (relative to dir) that LISTS the devices the
    #: module's vendor library can see, as one JSON line, opening none of them
    #: (Mission Control runs it in the module's own venv). "" = no probe.
    probe: str = ""
    #: THIS PC's choice (suite_local.json), passed as `address_arg address`
    #: when the module runs on real hardware; "" = the module's own config.
    address: str = ""
    #: A name safe for parameter ids and data files ("hf2", "hf2_lab2"). The id
    #: "hf2@lab2:5569" is fine for a settings file, but '@' and ':' do not
    #: belong in a recipe's parameter id or a netCDF variable name.
    slug: str = ""
    #: True for a module.toml in one of the suite's own projects (SUITE_PROJECTS,
    #: directly in the root): scan-core's scan server. Set by discover_local.
    suite_project: bool = False
    #: An INSTANCE: a second (third, ...) copy of a LOCAL module, run side by
    #: side with the first on this PC -- e.g. the scope module once for the
    #: Siglent and once with `--driver dwf` for an Analog Discovery. "" = the
    #: module itself. An instance has its own ports, real flag and address,
    #: plus `extra_args` for its service and optionally its own settings file
    #: (`config`, passed as --config). All of it is THIS PC's choice
    #: (suite_local.json, modules.<key>.instances); see add_instance.
    instance: str = ""
    extra_args: list = field(default_factory=list)
    config: str = ""

    @property
    def is_instance(self) -> bool:
        return bool(self.instance)

    @property
    def is_instrument(self) -> bool:
        """False for a service that DRIVES instruments (COORDINATOR_KEYS): a
        scan engine that follows the launcher connects to instruments only."""
        return self.key not in COORDINATOR_KEYS

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
        """The LOCAL module of this type (remote copies and instances have
        other ids)."""
        return next((m for m in self.modules
                     if m.key == key and not m.remote and not m.instance), None)


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
    hw = data.get("hardware") or {}
    address_arg = str(hw.get("address_arg", "")).strip()
    address_bus = str(hw.get("bus", "")).strip().lower()
    if address_arg and not address_arg.startswith("--"):
        raise ManifestError(f"{path}: [hardware] address_arg {address_arg!r} must be "
                            f"a flag of the service script, like \"--visa\"")
    if address_arg and address_bus not in ADDRESS_BUSES:
        raise ManifestError(f"{path}: [hardware] bus {address_bus!r} is not one of "
                            + ", ".join(ADDRESS_BUSES))
    probe_rel = str(hw.get("probe", "")).strip()
    if probe_rel and not (folder / probe_rel).is_file():
        raise ManifestError(f"{path}: [hardware] probe script {probe_rel!r} does not exist")

    return ModuleSpec(
        id=key, key=key, name=name,
        description=str(mod.get("description", "")).strip(),
        order=int(mod.get("order", 100)),
        dir=folder, icon=icon,
        cmd=cmd, pub=pub, default_cmd=cmd, default_pub=pub,
        service=service, gui=gui,
        start_after=[str(k) for k in after],
        category=category, tags=[t.strip() for t in tags if t.strip()],
        address_arg=address_arg, address_bus=address_bus if address_arg else "",
        probe=probe_rel,
    )


def manifest_paths(root: Path) -> list[Path]:
    """Every module.toml of a suite root, in the order discovery trusts them.

    Since 2026-09-27 a module lives at <root>/modules/<category>/<folder>/
    (the canonical layout, searched FIRST). A module.toml directly one level
    below the root is still found -- that is the layout before the move, and
    what somebody gets by dropping a module folder into the root by hand -- but
    it comes second, so when the same module is in both places the new copy
    wins (discover_local reports the old one).

    THE one place that knows where modules sit: discovery, the launcher's
    rescan, the tools and the installer generator all call this.
    """
    root = Path(root)
    nested = sorted(root.glob(f"{MODULES_DIR}/*/*/{MANIFEST}"))
    flat = sorted(root.glob(f"*/{MANIFEST}"))
    return nested + flat


def module_home(root: Path, category: str, folder: str) -> Path:
    """Where a module with this category and folder name belongs."""
    return Path(root) / MODULES_DIR / category / folder


def is_suite_project(root: Path, folder: Path) -> bool:
    """True for one of the suite's own projects (SUITE_PROJECTS) in `root`."""
    try:
        f = Path(folder).resolve()
        return f.parent == Path(root).resolve() and f.name in SUITE_PROJECTS
    except OSError:
        return False


def is_legacy_location(root: Path, folder: Path) -> bool:
    """True for a module folder sitting directly in the root (the old layout).

    Not for a suite project (scan-core): that one belongs in the root -- a
    deliberate exception, not a module waiting to be moved."""
    try:
        return (Path(folder).resolve().parent == Path(root).resolve()
                and not is_suite_project(root, folder))
    except OSError:
        return False


def rel_to_root(root: Path, folder: Path) -> str:
    """`folder` relative to the suite root with / separators
    ("modules/motion/kim-control"), or the absolute path if it is outside."""
    try:
        return Path(folder).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(folder)


def discover_local(root: Path) -> tuple[list[ModuleSpec], list[str]]:
    """Every module under `root` (see manifest_paths for where).

    A key found twice is a PROBLEM, never silently resolved: the copy under
    modules/ is used (it is searched first) and the message names both folders.
    The usual cause is an old <key>-control folder left in the root after the
    move to modules/ -- tools/migrate_layout.py tidies that up.
    """
    root = Path(root)
    specs, problems, seen = [], [], {}
    for path in manifest_paths(root):
        try:
            spec = parse_manifest(path)
        except ManifestError as exc:
            problems.append(str(exc))
            continue
        where = rel_to_root(root, path.parent)
        if spec.key in seen:
            hint = (" (an old folder left over from before the move to modules/? "
                    "run tools/migrate_layout.py)"
                    if is_legacy_location(root, path.parent) else "")
            problems.append(f"module {spec.key!r} exists twice: {seen[spec.key]} and "
                            f"{where}; using {seen[spec.key]}, ignoring {where}{hint}")
            continue
        seen[spec.key] = where
        spec.suite_project = is_suite_project(root, path.parent)
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
        replace_retry(tmp, root / LOCAL_FILE)   # Windows may refuse it for a moment
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_ports(key: str, cmd: int | None, pub: int | None,
              root: Path | None = None) -> None:
    """Override a local module's ports on this PC; None, None = back to default.

    `key` may also be an instance id ("scope#dwf"); an instance HAS no default
    ports (its ports are what it was added with), so None is refused there."""
    data = load_local(root)
    entry = _local_entry(data, key)
    if cmd is None:
        if split_id(key)[1]:
            raise ValueError("an instance has no default ports to go back to")
        entry.pop("cmd", None)
        entry.pop("pub", None)
    else:
        entry["cmd"], entry["pub"] = int(cmd), int(pub if pub is not None else cmd + 1)
    save_local(data, root)


def set_real(key: str, real: bool, root: Path | None = None) -> None:
    """Real hardware or simulator for a local module (or an instance id)."""
    data = load_local(root)
    _local_entry(data, key)["real"] = bool(real)
    save_local(data, root)


def set_address(key: str, address: str | None, root: Path | None = None) -> None:
    """Which instrument a local module opens on this PC (passed to its service
    with the module's [hardware] address_arg); None or "" = its own config.
    `key` may be an instance id: each copy opens its own instrument."""
    data = load_local(root)
    entry = _local_entry(data, key)
    if address:
        entry["address"] = str(address).strip()
    else:
        entry.pop("address", None)
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


# ------------------------------------------------------ module instances
#
# Lukas 2026-10-11: run two copies of ONE module side by side -- the scope
# module for the Siglent AND for the Analog Discovery 2 (`--driver dwf`). A
# module.toml can only describe one service, so the extra copies are THIS PC's
# choice, kept beside the module's other local settings in suite_local.json:
#
#     "modules": {"scope": {"real": true,
#                           "instances": [{"name": "dwf", "cmd": 5635, "pub": 5636,
#                                          "args": ["--driver", "dwf"],
#                                          "config": "", "real": true, "address": ""}]}}
#
# Each instance becomes a ModuleSpec of its own: id "scope#dwf", slug
# "scope_dwf" (gotcha #20: two copies of one module need distinct prefixes,
# so the scan engine registers scope.* AND scope_dwf.*). Nothing here stops
# two copies from opening the SAME instrument; the address lock does: the
# second real backend is refused its address (hwlock, gotcha #37) and exits
# with code 4, which the launcher shows on the card.

def instance_id(key: str, name: str) -> str:
    return f"{key}{INSTANCE_SEP}{name}"


def split_id(id: str) -> tuple[str, str]:
    """(key, instance name) of a LOCAL id: "scope" -> ("scope", ""),
    "scope#dwf" -> ("scope", "dwf")."""
    key, _sep, name = str(id).partition(INSTANCE_SEP)
    return key, name


def parse_args_text(text: str) -> list[str]:
    """Extra arguments typed on one line ("--driver dwf --dwf-device #1") ->
    a list, split the way a command line is (quotes group words)."""
    try:
        return shlex.split(str(text or ""), posix=True)
    except ValueError as exc:                       # e.g. an unclosed quote
        raise ValueError(f"cannot read the arguments: {exc}") from None


def args_text(args) -> str:
    """The inverse of parse_args_text, to show an instance's arguments."""
    return " ".join(shlex.quote(str(a)) for a in (args or []))


def check_instance_args(args) -> list[str]:
    """The extra arguments as a clean list of strings, or ValueError naming a
    flag the launcher sets itself (RESERVED_ARGS)."""
    out = [str(a) for a in (args or [])]
    for a in out:
        flag = a.split("=", 1)[0]
        if flag in RESERVED_ARGS:
            raise ValueError(f"{flag} is set by the launcher (ports / real / the "
                             f"settings-file field), not in the extra arguments")
    return out


def _instances(entry) -> list:
    inst = entry.get("instances") if isinstance(entry, dict) else None
    return inst if isinstance(inst, list) else []


def add_instance(key: str, name: str, cmd: int, pub: int | None = None,
                 args=(), config: str = "", root: Path | None = None) -> str:
    """Remember another copy of the local module `key` on this PC. Returns
    its id ("scope#dwf").

    `args` go to its service after the launcher's own (e.g. ["--driver",
    "dwf"]); `config` is a settings file (relative to the module's folder, or
    absolute) passed as `--config` -- for a module whose service takes one.
    The ports are NOT checked against the other modules here: discover()
    reports a clash as a problem, and the launcher's dialog refuses one first.
    """
    if not _KEY.match(key or ""):
        raise ValueError(f"module key {key!r} is not valid")
    name = str(name or "").strip()
    if not _INSTANCE.match(name):
        raise ValueError(f"instance name {name!r}: letters, digits and _ only "
                         f"(it becomes part of the parameter ids, e.g. "
                         f"{key}_{name or 'b'}.x)")
    cmd = int(cmd)
    pub = int(pub) if pub is not None else cmd + 1
    for p in (cmd, pub):
        if not 1024 <= p <= 65535:
            raise ValueError(f"port {p} outside 1024..65535")
    if cmd == pub:
        raise ValueError("the two ports must differ")
    args = check_instance_args(args)
    data = load_local(root)
    entry = data["modules"].setdefault(key, {})
    if not isinstance(entry.get("instances"), list):
        entry["instances"] = []
    if any(isinstance(i, dict) and str(i.get("name", "")).lower() == name.lower()
           for i in entry["instances"]):
        raise ValueError(f"{instance_id(key, name)} is already in the list")
    item = {"name": name, "cmd": cmd, "pub": pub, "args": args}
    if str(config or "").strip():
        item["config"] = str(config).strip()
    entry["instances"].append(item)
    save_local(data, root)
    return instance_id(key, name)


def remove_instance(id: str, root: Path | None = None) -> bool:
    """Forget an instance. Returns False if there was none with this id."""
    key, name = split_id(id)
    if not name:
        return False
    data = load_local(root)
    entry = data["modules"].get(key)
    items = _instances(entry)
    keep = [i for i in items if not (isinstance(i, dict) and i.get("name") == name)]
    if len(keep) == len(items):
        return False
    if keep:
        entry["instances"] = keep
    else:
        entry.pop("instances", None)
    save_local(data, root)
    return True


def _local_entry(data: dict, id: str) -> dict:
    """The suite_local.json dict a LOCAL id's settings live in: the module's
    own for "scope", the instance's for "scope#dwf"."""
    key, name = split_id(id)
    entry = data["modules"].setdefault(key, {})
    if not name:
        return entry
    for i in _instances(entry):
        if isinstance(i, dict) and i.get("name") == name:
            return i
    raise ValueError(f"no instance {id!r} on this PC")


def _instance_spec(base: ModuleSpec, item) -> tuple[ModuleSpec | None, str]:
    """The ModuleSpec of one instance entry of `base`, or (None, why)."""
    if not isinstance(item, dict):
        return None, "not an object"
    name = str(item.get("name", "")).strip()
    if not _INSTANCE.match(name):
        return None, f"bad name {name!r}"
    try:
        cmd = int(item["cmd"])
        pub = int(item.get("pub", cmd + 1))
    except (KeyError, TypeError, ValueError):
        return None, "no valid ports"
    try:
        args = check_instance_args(item.get("args") or [])
    except ValueError as exc:
        return None, str(exc)
    return replace(
        base,
        id=instance_id(base.key, name), name=f"{base.name} ({name})",
        # the instance's own ports ARE its defaults: there is nothing to reset to
        cmd=cmd, pub=pub, default_cmd=cmd, default_pub=pub,
        real=bool(item.get("real", False)),
        address=str(item.get("address", "") or "").strip() if base.address_arg else "",
        instance=name, extra_args=args, config=str(item.get("config", "") or "").strip(),
        start_after=list(base.start_after), tags=list(base.tags),
    ), ""


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
        m.address = str(over.get("address", "") or "").strip() if m.address_arg else ""

    instances = []
    for m in local:
        names = set()
        for item in _instances(settings["modules"].get(m.key)):
            spec, why = _instance_spec(m, item)
            if spec is not None and spec.instance.lower() in names:
                spec, why = None, "its name is used twice"
            if spec is None:
                problems.append(f"{LOCAL_FILE}: an instance of {m.key} was skipped: "
                                f"{why} ({item!r})")
                continue
            names.add(spec.instance.lower())
            instances.append(spec)
    installed = {m.key for m in local}
    for key, entry in settings["modules"].items():
        if key not in installed and _instances(entry):
            problems.append(f"{LOCAL_FILE}: instances of module {key!r} are set up, "
                            f"but that module is not installed on this PC; skipped")

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

    # an instance sits right after its module (same order, same base name)
    base_name = {m.key: m.name.lower() for m in local}
    modules = sorted(local + instances,
                     key=lambda m: (m.order, base_name[m.key], m.instance.lower()))
    modules += sorted(remote, key=lambda m: (m.order, m.name.lower(), m.id))
    _assign_slugs(modules)
    problems += port_conflicts(modules)
    return Discovery(modules=modules, problems=problems, root=root)


def _assign_slugs(modules: list[ModuleSpec]) -> None:
    """key for local modules; key_name for an instance ("scope_dwf"); key_host
    for remote ones (key_host_port if that is still ambiguous). Unique within
    the list: module keys are claimed first, then instances, then remotes."""
    taken = {m.key for m in modules if not m.remote and not m.instance}
    for m in modules:
        if not m.remote and not m.instance:
            m.slug = m.key
    for m in modules:
        if m.instance and not m.remote:
            slug = f"{m.key}_{m.instance}"
            if slug in taken:                 # e.g. another module's key "scope_dwf"
                slug = f"{slug}_{m.cmd}"
            m.slug = slug
            taken.add(slug)
    for m in modules:
        if not m.remote:
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
        # the instrument this PC chose for it (Instruments on this PC); only
        # with --real: the simulator opens nothing
        if m.address and m.address_arg:
            args += [m.address_arg, m.address]
    if m.instance:
        # an instance's own settings file, so two copies do not overwrite each
        # other's .ini; a relative name means inside the module's folder
        if m.config:
            cfg = Path(m.config)
            if not cfg.is_absolute() and m.dir is not None:
                cfg = m.dir / cfg
            args += ["--config", str(cfg)]
        args += list(m.extra_args)           # e.g. --driver dwf
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
    (camera -> kim) can find it by type. Remote ones and instances only under
    their full id ("kim@lab2:5567", "scope#dwf"): neither may silently replace
    the local module of that type.
    """
    table = {}
    for m in modules:
        name = m.id if (m.remote or m.instance) else m.key
        table[name] = [m.host, m.cmd, m.pub]
    return json.dumps(table)
