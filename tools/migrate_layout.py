"""Tidy up a checkout after the move to modules/<category>/ (2026-09-27).

    python tools/migrate_layout.py            # DRY RUN: only says what it would do
    python tools/migrate_layout.py --apply    # do it

WHY THIS EXISTS. On 2026-09-27 every instrument module moved from
<root>/<key>-control to <root>/modules/<category>/<key>-control. `git pull`
moves every file git TRACKS. It does not touch the files git ignores or does
not know -- and those are exactly the ones a rig cares about: a module's tuned
.ini, its Calibrations folder, px_calibration.json, private CLAUDE.local.md
notes, data and logs. After the pull they are still in the OLD folder, which
now holds nothing else, and the module (running from the new folder) no longer
sees them. This script moves them across.

What it does, for every module that has a new folder AND an old one:
  * moves each remaining file of the old folder to the same place in the new
    folder. It NEVER overwrites: if the new folder already has a DIFFERENT file
    of that name, the old one is kept beside it as <name>.old-layout, and the
    report tells you to compare the two (an identical file is simply dropped).
  * deletes what is rebuilt anyway: .venv, *.egg-info, __pycache__,
    .pytest_cache (a .venv there points at the OLD src folder and is useless).
  * removes the old folder if it is then empty, and says so if it is not
    (e.g. a file was locked because a service was still running).
Then rebuild each module's environment (the editable install in it still
points at the old folder) -- the script prints the commands.

BEFORE `git pull` on a rig: some lab files are TRACKED (camera-control's
camera.ini and objectives.ini, kim's px_calibration.json, clMag's
Calibrations). If you changed them locally, `git pull` refuses to move the
folder, or moves the committed version and not yours. So first either commit
them (`git add ... ; git commit`) or put them aside with `git stash`, then
`git pull`, then `git stash pop` (git then applies your edits to the files at
their new place). Only after that run this script.

Standard library only; run it with any Python 3.11+ from the repo root. Output
is ASCII (docs/DEVELOPER_NOTES.md gotcha #14).
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "suite-common" / "src"))
from suite_common.catalog import is_lab_data                          # noqa: E402
from suite_common.modules import (MANIFEST, discover_local,            # noqa: E402
                                  is_legacy_location, rel_to_root)

#: Rebuilt by `uv sync` / Python / pytest: deleted, never moved.
REGENERABLE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SUFFIX = ".old-layout"


def _regenerable(name: str) -> bool:
    return name in REGENERABLE_DIRS or name.endswith(".egg-info")


def kind_of(rel: str) -> str:
    """What a leftover file is, for the report (all of them are moved)."""
    low = rel.lower()
    if is_lab_data(rel) or low.endswith("px_calibration.json"):
        return "lab data"
    if rel.rsplit("/", 1)[-1] == "CLAUDE.local.md":
        return "private notes"
    if low.endswith((".csv", ".log", ".nc", ".txt", ".dat")) or rel.startswith("out/"):
        return "data/log"
    return "other"


@dataclass
class Step:
    what: str          # "move" | "keep-both" | "same" | "delete" | "rmdir" | "skip" | "error"
    src: str
    dst: str = ""
    note: str = ""


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)     # new module folders that got files

    def count(self, what: str) -> int:
        return sum(1 for s in self.steps if s.what == what)


def _git_tracked(root: Path, rel: str) -> list[str]:
    """Files git still tracks under `rel` ([] if git is not available)."""
    try:
        out = subprocess.run(["git", "ls-files", "--", rel], cwd=root, capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [x for x in out.stdout.splitlines() if x.strip()] if out.returncode == 0 else []


def _free_name(path: Path) -> Path:
    """path + .old-layout, or .old-layout2, 3, ... if that is taken too."""
    cand = path.with_name(path.name + SUFFIX)
    n = 2
    while cand.exists():
        cand = path.with_name(f"{path.name}{SUFFIX}{n}")
        n += 1
    return cand


def migrate(root: Path, apply: bool = False) -> Report:
    """Plan (apply=False) or carry out (apply=True) the tidy-up of `root`."""
    root = Path(root)
    rep = Report()
    mods, _problems = discover_local(root)
    for m in mods:
        if is_legacy_location(root, m.dir):
            # a module that is ONLY in the old place: nothing to merge into
            if not any(x.key == m.key and not is_legacy_location(root, x.dir) for x in mods):
                rep.steps.append(Step("skip", m.dir.name, note=(
                    f"is still a module in the old place (it has a {MANIFEST}) and there "
                    f"is no modules/{m.category}/{m.dir.name} -- pull first, or move it "
                    "with git mv")))
            continue
        old = root / m.dir.name
        if not old.is_dir() or old.resolve() == m.dir.resolve():
            continue
        new = m.dir
        new_rel = rel_to_root(root, new)
        if (old / MANIFEST).is_file():
            rep.steps.append(Step("skip", old.name, note=(
                f"still holds a {MANIFEST}: it is a whole second copy of the module, "
                f"not leftovers. Compare it with {new_rel} and delete one by hand.")))
            continue
        tracked = _git_tracked(root, old.name)
        if tracked:
            rep.steps.append(Step("skip", old.name, note=(
                f"git still tracks {len(tracked)} file(s) there (e.g. {tracked[0]}): "
                "finish the pull (or git stash pop) first")))
            continue

        moved_any = False
        for dirpath, dirnames, filenames in os.walk(old, topdown=True):
            here = Path(dirpath)
            keep = []
            for d in sorted(dirnames):
                if _regenerable(d):
                    target = here / d
                    rel = target.relative_to(root).as_posix()
                    if apply:
                        try:
                            shutil.rmtree(target)
                            rep.steps.append(Step("delete", rel, note="rebuilt by uv / Python"))
                        except OSError as exc:
                            rep.steps.append(Step("error", rel, note=f"could not delete: {exc} "
                                                  "(a running service? stop it and re-run)"))
                    else:
                        rep.steps.append(Step("delete", rel, note="rebuilt by uv / Python"))
                else:
                    keep.append(d)
            dirnames[:] = keep                   # never walk into what is deleted
            for f in sorted(filenames):
                src = here / f
                inner = src.relative_to(old).as_posix()
                dst = new.joinpath(*inner.split("/"))
                src_rel = src.relative_to(root).as_posix()
                dst_rel = dst.relative_to(root).as_posix()
                what = kind_of(inner)
                if dst.exists():
                    try:
                        same = dst.is_file() and filecmp.cmp(src, dst, shallow=False)
                    except OSError:
                        same = False
                    if same:
                        rep.steps.append(Step("same", src_rel, dst_rel, what))
                        if apply:
                            src.unlink()
                        continue
                    dst = _free_name(dst)
                    dst_rel = dst.relative_to(root).as_posix()
                    rep.steps.append(Step("keep-both", src_rel, dst_rel, what))
                else:
                    rep.steps.append(Step("move", src_rel, dst_rel, what))
                moved_any = True
                if apply:
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dst))
                    except OSError as exc:
                        rep.steps[-1] = Step("error", src_rel, dst_rel,
                                             f"could not move: {exc}")
        if moved_any:
            rep.touched.append(new_rel)

        if apply:
            # remove the now-empty folders, deepest first, then the old folder
            for dirpath, _dirs, _files in sorted(os.walk(old, topdown=False),
                                                 key=lambda t: -len(Path(t[0]).parts)):
                try:
                    Path(dirpath).rmdir()
                except OSError:
                    pass
            if old.exists():
                left = sum(1 for p in old.rglob("*") if p.is_file())
                rep.steps.append(Step("error", old.name, note=(
                    f"not removed: {left} file(s) are still in it (locked?)")))
            else:
                rep.steps.append(Step("rmdir", old.name, note=f"now lives in {new_rel}"))
        else:
            rep.steps.append(Step("rmdir", old.name, note=f"now lives in {new_rel}"))
    return rep


def print_report(rep: Report, apply: bool) -> None:
    will = "" if apply else "would "
    labels = {"move": f"{will}move", "keep-both": f"{will}KEEP BOTH",
              "same": f"{will}drop (identical)", "delete": f"{will}delete",
              "rmdir": f"{will}remove folder", "skip": "SKIPPED", "error": "ERROR"}
    for s in rep.steps:
        line = f"  {labels[s.what]:<24} {s.src}"
        if s.dst:
            line += f"  ->  {s.dst}"
        if s.note:
            line += f"   [{s.note}]"
        print(line)
    if not rep.steps:
        print("  nothing to do: no old <key>-control folder is left next to modules/.")
        return
    both = rep.count("keep-both")
    print()
    print(f"{rep.count('move')} file(s) {will}move, {both} kept side by side, "
          f"{rep.count('same')} identical, {rep.count('delete')} folder(s) {will}delete, "
          f"{rep.count('skip')} skipped, {rep.count('error')} error(s)")
    if both:
        print(f"KEEP BOTH: the new folder already had a DIFFERENT file of that name. Yours "
              f"is kept as *{SUFFIX}; compare the two and keep the right one (e.g. the "
              "rig's tuned .ini).")
    if not apply:
        print("This was a dry run. Run again with --apply to do it.")
    elif rep.touched or rep.count("delete"):
        print()
        print("Now rebuild the environments -- the installed module still points at the")
        print("old folder. For each module, from its NEW folder:")
        for rel in rep.touched or ["modules/<category>/<key>-control"]:
            print(f"    cd {rel}")
            print("    uv sync --extra gui          (add --extra real if its pyproject has one;")
            print("                                  on a OneDrive tree use .\\dev.ps1 sync ...)")
        print("or run tools\\deploy_lab.ps1 -Target . -NoClone, which syncs and tests everything.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="do it (default: dry run)")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    print(f"{'Tidying' if args.apply else 'Dry run for'} {args.root}")
    rep = migrate(args.root, apply=args.apply)
    print_report(rep, args.apply)
    return 1 if rep.count("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
