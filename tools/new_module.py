"""Create a new instrument module from a template, ready to run.

    python tools/new_module.py vna --name "Network analyser" --description "R&S ZNB 20 GHz"
    python tools/new_module.py lockin2 --like hf2

What it does, so you know what you get:

1. Copies a working module (default `smb-control`, the set-and-forget
   template; `--like clMag` for a closed-loop one, `--like hf2` for a detector)
   to `<key>-control/`, skipping its venv, caches, lock file and local .ini.
2. Renames the package (`src/smb` -> `src/vna`), its class-name prefix
   (`SmbService` -> `VnaService`) and every import.
3. Takes the NEXT FREE port pair from the suite's scheme (5555 + 2n), checked
   against every module.toml so it cannot clash.
4. Writes `module.toml` (the launcher shows the module at once), a placeholder
   `icon.svg`, and stub README.md / CLAUDE.local.md (private notes, not in git) saying what is still template.

The copy RUNS and its tests PASS before you change anything: it is the template
instrument under a new name. Then you replace its insides -- config, backends,
brain, describe -- keeping the shape. INSTRUMENT_MODULE_GUIDE.md walks through it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "suite-common" / "src"))
from suite_common.modules import CATEGORIES, MANIFEST, discover_local  # noqa: E402

SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", "out", ".suite_cache"}
SKIP_FILES = {"uv.lock", "CLAUDE.md", "CLAUDE.local.md", "README.md", "module.toml", "icon.svg"}
TEXT_SUFFIXES = {".py", ".toml", ".md", ".txt", ".cfg", ".json", ".gitignore", ".ps1"}

ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" width="40" height="40">
  <!-- placeholder icon: replace with a glyph that reads as your instrument.
       Draw in #ff9e2c (accent), #ffb454 (accent_hi), #e8eaed (text) and
       #5a626e (metal); the launcher swaps the first three for the active theme. -->
  <rect x="7" y="7" width="26" height="26" rx="6" fill="none" stroke="#5a626e" stroke-width="2.4"/>
  <circle cx="20" cy="20" r="6" fill="none" stroke="#ff9e2c" stroke-width="2.4"/>
  <circle cx="20" cy="20" r="2" fill="#ffb454"/>
</svg>
"""


def next_free_ports(root: Path) -> tuple[int, int]:
    """The first pair 5555 + 2n that no module.toml uses (cmd or pub)."""
    used = set()
    for m in discover_local(root)[0]:
        used.update((m.cmd, m.pub))
    n = 0
    while True:
        cmd = 5555 + 2 * n
        if cmd not in used and cmd + 1 not in used:
            return cmd, cmd + 1
        n += 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key", help="short lowercase name, also the Python package (e.g. vna)")
    ap.add_argument("--like", default="smb", help="template module key (default: smb)")
    ap.add_argument("--name", default=None, help="display name in the launcher")
    ap.add_argument("--description", default="", help="one line for the launcher card")
    ap.add_argument("--category", default="other", choices=list(CATEGORIES),
                    help="what the module is FOR (the Add module wizard filters on it)")
    ap.add_argument("--tags", default="",
                    help='comma-separated search words, e.g. "lock-in,Zurich Instruments"')
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    root: Path = args.root
    key = args.key
    if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
        print(f"key {key!r}: use lowercase letters, digits and _ (it becomes a Python package)")
        return 2
    local = {m.key: m for m in discover_local(root)[0]}
    if key in local:
        print(f"a module with key {key!r} already exists: {local[key].dir}")
        return 2
    template = local.get(args.like)
    if template is None:
        print(f"no template module {args.like!r}; available: {', '.join(sorted(local))}")
        return 2
    dest = root / f"{key}-control"
    if dest.exists():
        print(f"{dest} already exists")
        return 2

    old = template.key
    old_pkg_dir = template.dir / "src" / old
    if not old_pkg_dir.is_dir():
        print(f"template {old!r} does not have the src/{old} layout; pick another --like")
        return 2
    cmd, pub = next_free_ports(root)
    Cap, OldCap = key[:1].upper() + key[1:], old[:1].upper() + old[1:]

    def ignore(folder, names):
        # <old>.ini is the template's SAVED STATE (camera.ini holds a calibrated
        # spot); other .ini files, like camera's objectives.ini, are real config.
        return [n for n in names
                if n in SKIP_DIRS or n in SKIP_FILES or n.endswith(".egg-info")
                or n == f"{old}.ini"]

    shutil.copytree(template.dir, dest, ignore=ignore)
    (dest / "src" / old).rename(dest / "src" / key)

    # rename files whose name carries the old key, and the console, which the
    # guide calls scripts/<key>_console.py (smb's is historically rf_console.py)
    for path in sorted(dest.rglob("*"), key=lambda p: -len(p.parts)):
        if not path.is_file():
            continue
        if path.parent.name == "scripts" and path.name.endswith("_console.py"):
            path.rename(path.with_name(f"{key}_console.py"))
        elif old in path.name:
            path.rename(path.with_name(path.name.replace(old, key)))

    replacements = [
        (re.compile(rf"\b{re.escape(old)}-control\b"), f"{key}-control"),
        (re.compile(rf"\b{re.escape(OldCap)}(?=[A-Z])"), Cap),     # SmbService -> VnaService
        (re.compile(rf"\b{re.escape(old)}\b"), key),               # imports, "module": "smb"
        (re.compile(rf"\b{template.default_cmd}\b"), str(cmd)),
        (re.compile(rf"\b{template.default_pub}\b"), str(pub)),
    ]
    changed = 0
    for path in dest.rglob("*"):
        if not path.is_file() or (path.suffix not in TEXT_SUFFIXES and path.name != ".gitignore"):
            continue
        text = path.read_text("utf-8")
        new = text
        for pattern, repl in replacements:
            new = pattern.sub(repl, new)
        if new != text:
            path.write_text(new, "utf-8")
            changed += 1

    name = args.name or key.upper()
    today = dt.date.today().isoformat()
    gui = "scripts/run_gui.py" if (dest / "scripts" / "run_gui.py").is_file() else ""
    order = max([m.order for m in local.values()] + [0]) + 10
    (dest / MANIFEST).write_text(f"""# module.toml -- how the suite finds this module (INSTRUMENT_MODULE_GUIDE.md section 11).
# Identity only; the variables come from the running service's `describe`.

[module]
key = "{key}"
name = "{name}"
description = "{args.description}"
category = "{args.category}"
tags = [{", ".join(f'"{t.strip()}"' for t in args.tags.split(",") if t.strip())}]
icon = "icon.svg"
order = {order}

[ports]
cmd = {cmd}
pub = {pub}

[run]
service = "scripts/run_service.py"
gui = "{gui}"
start_after = []
""", "utf-8")
    (dest / "icon.svg").write_text(ICON, "utf-8")
    (dest / "README.md").write_text(f"""# {key}-control -- {name}

Created {today} by `tools/new_module.py` from the `{old}` template. **Until you
replace its insides it is still the {old} instrument under a new name.**

Ports {cmd} / {pub}.

```powershell
cd {key}-control
uv sync --extra gui
uv run pytest -q
uv run scripts/run_service.py
uv run scripts/run_gui.py --connect localhost
```
""", "utf-8")
    (dest / "CLAUDE.local.md").write_text(f"""# {key}-control -- module memory (Claude Code)

> Suite overview in `..\\docs\\DEVELOPER_NOTES.md`; module contract in `..\\INSTRUMENT_MODULE_GUIDE.md`.

Created {today} with `tools/new_module.py {key} --like {old}`: a copy of
`{old}-control` with the package, class prefix and ports renamed. Ports {cmd} / {pub}.

## Still template (replace)
- `src/{key}/config.py`, `backends/`, the brain, `net/describe.py`, the GUI's
  indicator widget and every docstring that still describes the {old} instrument.
- `icon.svg` is a placeholder.

## Status
- Generated; tests pass as generated. Nothing instrument-specific yet.
""", "utf-8")

    print(f"created {dest.relative_to(root)}  (from {old}, ports {cmd}/{pub}, {changed} files rewritten)")
    print("next:")
    print(f"  cd {key}-control")
    print("  uv sync --extra gui")
    print("  uv run pytest -q                  # passes before you change anything")
    print("  python ../tools/check_modules.py  # contract check")
    print("The launcher lists it already (Rescan, or wait a few seconds).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
