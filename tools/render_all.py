"""Refresh every front-panel PNG in one go.

Each panel has to be rendered from inside its own project, because that is where
its package and its PySide6 live. This just walks the list and shells out to
`uv run` in the right directory, so refreshing the whole gallery after a GUI
change is one command:

    python tools/render_all.py
    python tools/render_all.py --theme light --suffix -light
    python tools/render_all.py clMag kim          # just these

Note it does NOT set UV_PROJECT_ENVIRONMENT. On this lab PC the environments
live outside OneDrive (see docs/DEVELOPER_NOTES.md section 2); pass that through if you use
that layout:

    UV_PROJECT_ENVIRONMENT=... python tools/render_all.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Module panels are FOUND (module discovery), not listed: every module with a
# GUI gets a target named after its key. suite-common is imported straight from
# its folder, so this script still runs with any plain Python.
sys.path.insert(0, str(ROOT / "suite-common" / "src"))
from suite_common.modules import discover_local, rel_to_root  # noqa: E402

#: target name -> (project directory relative to the root, e.g.
#: "modules/motion/kim-control", does it need the `gui` extra?)
PROJECTS = {m.key: (rel_to_root(ROOT, m.dir), True) for m in discover_local(ROOT)[0]
            if m.gui and not m.suite_project}   # scan-core's scan server: suite-watch
PROJECTS.update({
    # the windows that are not instrument modules
    "scan-core": ("scan-core", True),
    # The measurement suite: one target per tab, because a tabbed app only ever
    # shows one at a time. Set SUITE_RENDER_MODULES=clMag,smb,piezo to render
    # the Control tab against live services instead of the simulator.
    "suite-control": ("scan-core", True),
    "suite-scan": ("scan-core", True),
    "suite-measurement": ("scan-core", True),
    # the Measurement tab WATCHING a scan server (a scan running in another
    # process -- here an in-process server on the simulator)
    "suite-watch": ("scan-core", True),
    "suite-watch-remote": ("scan-core", True),
    "suite-data": ("scan-core", True),
    "suite-settings": ("scan-core", True),
    "suite-catalogue": ("scan-core", True),
    "suite-queue": ("scan-core", True),
    "suite-queue-dialog": ("scan-core", True),
    "suite-fly-scan": ("scan-core", True),
    "suite-repeat-scan": ("scan-core", True),
    "suite-scout-scan": ("scan-core", True),
    "suite-scout": ("scan-core", True),
    "suite-fly": ("scan-core", True),
    # an axis row's Advanced panel opened in place (fly, and scout)
    "suite-axis-advanced": ("scan-core", True),
    "suite-axis-advanced-scout": ("scan-core", True),
    # the data viewer (AaltoView successor), on simulated measurements
    "viewer-map": ("scan-core", True),
    "viewer-1d": ("scan-core", True),
    "mission-control": ("mission-control", False),   # PySide6 is a base dep
    "mission-control-instruments": ("mission-control", False),
    # a GUI connected to a service, with the control bar (holder / viewer);
    # rendered in smb's project because the pose is an smb window
    "control-holder": ("modules/source/smb-control", True),
    "control-viewer": ("modules/source/smb-control", True),
    # the scope's second tab (the XY / YX loop)
    "scope-xy": ("modules/detector/scope-control", True),
    "scope-ad": ("modules/detector/scope-control", True),
})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", default=None,
                    help="which panels (default: all)")
    ap.add_argument("--theme", default="dark", choices=("dark", "light"))
    ap.add_argument("--suffix", default="",
                    help="appended to the PNG name, e.g. -light")
    args = ap.parse_args()

    wanted = args.targets or list(PROJECTS)
    unknown = [t for t in wanted if t not in PROJECTS]
    if unknown:
        print(f"unknown target(s): {', '.join(unknown)}\n"
              f"known: {', '.join(PROJECTS)}")
        return 2

    failed = []
    for name in wanted:
        directory, needs_gui = PROJECTS[name]
        out = ROOT / "front-panels" / f"{name}{args.suffix}.png"
        cmd = ["uv", "run"]
        if needs_gui:
            cmd += ["--extra", "gui"]
        cmd += ["python", str(ROOT / "tools" / "render_panels.py"), name,
                "--theme", args.theme, "--out", str(out)]

        proc = subprocess.run(cmd, cwd=ROOT / directory, capture_output=True,
                              text=True)
        if proc.returncode == 0:
            print(proc.stdout.strip() or f"  {name}: rendered")
        else:
            failed.append(name)
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-4:]
            print(f"  {name:16s} FAILED")
            for line in tail:
                print(f"      {line}")

    if failed:
        print(f"\n{len(failed)} panel(s) failed: {', '.join(failed)}")
        return 1
    print(f"\n{len(wanted)} panel(s) up to date in front-panels/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
