"""pack_module.py -- put one or more modules into a MODULE PACK (.zip).

A pack is how a module travels to another AaltoFlow installation: Mission
Control's "Add module..." installs it from the zip. It holds the module
folders exactly as COMMITTED (git archive HEAD -- never the working tree, which
has .venv, caches and this rig's calibrations), plus a pack.json label.

With --wheels the pack also carries every Python package the modules need, as
wheel files, so the install needs NO internet -- the answer for a lab subnet
where PyPI is blocked. The versions are the ones pinned in each module's
uv.lock (exported, then downloaded), i.e. exactly what the tests ran against.
Wheels are platform-specific: they are fetched for Windows x64 and the Python
version the target will use (default: the one in this module's own .venv).

    python tools/pack_module.py hf2                      # code only (online install)
    python tools/pack_module.py hf2 pm16 --wheels        # + packages (offline install)
    python tools/pack_module.py vna --wheels --python 3.14 --out D:\\packs

Needs git, and for --wheels uv (+ internet on THIS machine).
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "suite-common" / "src"))

from suite_common.catalog import (find_uv, module_version, project_extras,  # noqa: E402
                                  requirements_name, write_pack_json)
from suite_common.modules import discover_local                             # noqa: E402


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True).stdout


def venv_python_version(folder: Path) -> str | None:
    """'3.14' from <folder>/.venv/pyvenv.cfg -- the Python the module is tested on."""
    cfg = folder / ".venv" / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    m = re.search(r"^version_info\s*=\s*(\d+)\.(\d+)", cfg.read_text("utf-8"), re.M)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def export_committed(folder: str, dest: Path) -> None:
    """The folder as it is in HEAD, unpacked into dest/<folder>."""
    data = git("archive", "--format=tar", "HEAD", folder)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(dest, filter="data")


def fetch_wheels(uv: str, folder: Path, key: str, wheels: Path, python: str) -> int:
    """Pinned requirements from uv.lock -> wheel files for Windows x64."""
    wheels.mkdir(exist_ok=True)
    req = wheels / requirements_name(key)
    extras = [a for x in project_extras(folder) for a in ("--extra", x)]
    subprocess.run([uv, "export", "--project", str(folder), "--frozen", "--no-hashes",
                    "--no-emit-project", "--no-dev", *extras,
                    "--format", "requirements-txt", "-o", str(req)],
                   check=True, capture_output=True)
    # The build backend too: the offline install puts the module in EDITABLE
    # with --no-build-isolation, so setuptools must already be in the .venv.
    with open(folder / "pyproject.toml", "rb") as fh:
        build = tomllib.load(fh).get("build-system", {}).get("requires", [])
    with open(req, "a", encoding="utf-8") as fh:
        fh.write("\n# build backend (offline editable install)\n")
        for r in build:
            fh.write(r + "\n")
    uvx = str(Path(uv).with_name("uvx.exe" if uv.endswith(".exe") else "uvx"))
    before = {p.name for p in wheels.glob("*.whl")}
    subprocess.run([uvx, "pip", "download", "-r", str(req), "-d", str(wheels),
                    "--only-binary=:all:", "--platform", "win_amd64",
                    "--python-version", python, "--implementation", "cp",
                    "--quiet"], check=True)
    return len({p.name for p in wheels.glob("*.whl")} - before)


class PackError(RuntimeError):
    pass


def make_pack(keys: list[str], wheels: bool = False, python: str | None = None,
              out_dir: Path = ROOT / "dist" / "modules", say=print) -> Path:
    """Build one pack of the given modules; returns the .zip. (Also used by
    tools/release_modules.py, once per module.)"""
    mods = {m.key: m for m in discover_local(ROOT)[0]}
    missing = [k for k in keys if k not in mods]
    if missing:
        raise PackError(f"unknown module(s): {', '.join(missing)} -- known: {', '.join(mods)}")
    chosen = [mods[k] for k in keys]

    commit = git("rev-parse", "--short", "HEAD").decode().strip()
    dirty = git("status", "--porcelain", "--", *[m.dir.name for m in chosen]).decode().strip()
    if dirty:
        say("note: uncommitted changes in these folders are NOT packed "
            "(the pack is built from the last commit):\n" + dirty)

    uv = None
    if wheels:
        uv = find_uv(ROOT)
        if uv is None:
            raise PackError("uv not found -- needed for --wheels")
        python = python or venv_python_version(chosen[0].dir)
        if python is None:
            raise PackError("which Python will the target use? pass --python 3.14")
    else:
        python = None

    with tempfile.TemporaryDirectory(prefix="aaltoflow_pack_") as tmp:
        stage = Path(tmp)
        for m in chosen:
            export_committed(m.dir.name, stage)
            say(f"{m.key}: {m.dir.name} @ {commit}")
            if wheels:
                n = fetch_wheels(uv, stage / m.dir.name, m.key, stage / "wheels", python)
                say(f"{m.key}: {n} new wheel files for Python {python}")
        packed = discover_local(stage)[0]
        write_pack_json(stage, packed, commit, python)

        name = "+".join(m.key for m in chosen)
        version = module_version(chosen[0].dir) if len(chosen) == 1 else ""
        stem = "-".join(x for x in (name, version, commit,
                                    "offline" if wheels else "") if x)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{stem}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(stage).as_posix())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keys", nargs="+", help="module keys, e.g. hf2 pm16")
    ap.add_argument("--wheels", action="store_true",
                    help="include every Python package (offline install)")
    ap.add_argument("--python", default=None,
                    help="target Python for the wheels, e.g. 3.14 (default: the module's .venv)")
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "modules")
    args = ap.parse_args(argv)
    try:
        out = make_pack(args.keys, args.wheels, args.python, args.out)
    except PackError as exc:
        print(exc)
        return 2
    print(f"\npack: {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    print("install it with Mission Control > Add module...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
