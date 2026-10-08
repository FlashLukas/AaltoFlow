"""tools/lab_backup.ps1 -Mode restore: a stale flat copy never beats the nested one.

Found on the lab PC (2026-10-08): the lab repo held BOTH
AaltoFlow/<key>-control/CLAUDE.local.md (a backup from before the move to
modules/<category>/) and AaltoFlow/modules/<category>/<key>-control/CLAUDE.local.md.
The restore maps the flat path onto the nested folder, so both landed on one
file and whichever was listed later won -- for seven modules the OLD notes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "lab_backup.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PS is None or not SCRIPT.exists(),
                                reason="needs PowerShell and tools/lab_backup.ps1")


def _setup(tmp_path: Path):
    """A fake checkout (tools/ + a nested module) and a lab repo with both copies.
    The flat folder is named so it sorts AFTER 'modules' -- the order that lost."""
    flow = tmp_path / "AaltoFlow"
    (flow / "tools").mkdir(parents=True)
    shutil.copy(SCRIPT, flow / "tools" / "lab_backup.ps1")
    (flow / "modules" / "detector" / "zz-control").mkdir(parents=True)
    lab = tmp_path / "aaltoflow-lab"
    subprocess.run(["git", "init", "-q", str(lab)], check=True)
    old = lab / "AaltoFlow" / "zz-control" / "CLAUDE.local.md"
    new = lab / "AaltoFlow" / "modules" / "detector" / "zz-control" / "CLAUDE.local.md"
    for p, txt in ((old, "OLD flat notes\n"), (new, "NEW nested notes\n")):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
    return flow, lab


def _run(flow: Path, *args) -> str:
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(flow / "tools" / "lab_backup.ps1"), *args, "-NoMemory",
                        "-ViewerRoot", str(flow.parent / "no-viewer")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_restore_force_keeps_the_nested_copy(tmp_path):
    flow, lab = _setup(tmp_path)
    out = _run(flow, "-Mode", "restore", "-Force")
    dest = flow / "modules" / "detector" / "zz-control" / "CLAUDE.local.md"
    assert dest.read_text(encoding="utf-8") == "NEW nested notes\n"
    assert "skipped old flat copy: zz-control" in out
    assert not (flow / "zz-control").exists()        # no flat folder recreated


def test_whatif_copies_nothing(tmp_path):
    flow, lab = _setup(tmp_path)
    out = _run(flow, "-Mode", "restore", "-Force", "-WhatIf")
    assert "would restore: modules\\detector\\zz-control\\CLAUDE.local.md" in out
    assert "nothing was copied" in out
    assert not (flow / "modules" / "detector" / "zz-control" / "CLAUDE.local.md").exists()
