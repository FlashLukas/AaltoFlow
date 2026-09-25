"""release_modules.py -- publish the modules as a GitHub release (the online catalog).

Mission Control's "Add module... > From online catalog" reads

    https://github.com/FlashLukas/AaltoFlow/releases/latest/download/catalog.json

-- the catalog of the NEWEST release (GitHub keeps that address pointing at the
latest one). This tool makes such a release from the last commit:

  * one module pack per module, code only (small; the Python packages come from
    PyPI when the environment is built on the target PC),
  * with --offline also one pack per module WITH its packages (~250 MB for a GUI
    module; for PCs that cannot reach PyPI),
  * catalog.json: every module, what it is for, and its downloads with size and
    SHA-256 -- the installer refuses a download whose hash does not match.

Download links in the catalog are RELATIVE to the catalog, so the whole release
folder can also be copied to a share and used as a mirror (point the catalog
address in Add module... at T:\\...\\catalog.json).

    python tools/release_modules.py --dry-run            # build into dist/release/<tag>/ only
    python tools/release_modules.py                      # build + publish (gh release create)
    python tools/release_modules.py --offline            # + the offline packs
    python tools/release_modules.py hf2 pm16 --dry-run   # only some modules

Publishing needs `gh` logged in with write access. A release made while the
repository is PRIVATE is not downloadable without a login -- it works for
everyone once the repository is public.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "suite-common" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pack_module import PackError, git, make_pack, venv_python_version  # noqa: E402
from suite_common.catalog import CATALOG_FILE, release_catalog            # noqa: E402
from suite_common.modules import discover_local                            # noqa: E402

REPO = "FlashLukas/AaltoFlow"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keys", nargs="*", help="modules to release (default: all)")
    ap.add_argument("--offline", action="store_true",
                    help="also publish packs that carry their Python packages")
    ap.add_argument("--python", default=None, help="target Python for --offline (default: "
                    "each module's .venv)")
    ap.add_argument("--dry-run", action="store_true", help="build, do not publish")
    ap.add_argument("--repo", default=REPO)
    args = ap.parse_args(argv)

    mods = {m.key: m for m in discover_local(ROOT)[0]}
    keys = args.keys or list(mods)
    unknown = [k for k in keys if k not in mods]
    if unknown:
        print("unknown module(s):", ", ".join(unknown))
        return 2
    if git("status", "--porcelain").decode().strip():
        print("note: the working tree has uncommitted changes; the release is built "
              "from the last commit only.")

    commit = git("rev-parse", "--short", "HEAD").decode().strip()
    date = git("show", "-s", "--format=%cd", "--date=format:%Y.%m.%d", "HEAD").decode().strip()
    tag = f"modules-{date}-{commit}"
    out = ROOT / "dist" / "release" / tag
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    assets: dict[str, dict[str, Path]] = {}
    python = None
    try:
        for k in keys:
            assets[k] = {"code": make_pack([k], out_dir=out, say=lambda m: None)}
            if args.offline:
                py = args.python or venv_python_version(mods[k].dir)
                python = python or py
                assets[k]["offline"] = make_pack([k], wheels=True, python=py, out_dir=out,
                                                 say=lambda m: None)
            sizes = ", ".join(f"{kind} {p.stat().st_size / 1e6:.1f} MB"
                              for kind, p in assets[k].items())
            print(f"{k:10} {sizes}")
    except (PackError, subprocess.CalledProcessError) as exc:
        print("FAILED:", exc)
        return 1

    doc = release_catalog([mods[k] for k in keys], assets, commit, tag, python)
    (out / CATALOG_FILE).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8")
    files = sorted(out.iterdir())
    total = sum(p.stat().st_size for p in files) / 1e6
    print(f"\n{tag}: {len(files)} files, {total:.1f} MB in {out}")
    if args.dry_run:
        print("dry run: nothing published. Test it: Add module... > From online catalog "
              f"> {out / CATALOG_FILE}")
        return 0

    notes = (f"Module packs of AaltoFlow at commit {commit}.\n\n"
             "Install them from Mission Control: **Add module... > From online catalog**. "
             "`catalog.json` lists every module with its downloads (size + SHA-256). "
             "Packs ending in `-offline.zip` carry their Python packages and install "
             "without internet.")
    cmd = ["gh", "release", "create", tag, *map(str, files), "--repo", args.repo,
           "--title", f"Modules {date} ({commit})", "--notes", notes, "--latest"]
    print("publishing:", " ".join(cmd[:4]), f"... ({len(files)} files)")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode == 0:
        print(f"published: https://github.com/{args.repo}/releases/tag/{tag}")
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
