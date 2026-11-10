"""Unpack Thorlabs' Python camera SDK (thorlabs_tsi_sdk) where the camera's
"thorlabs" driver finds it -- once per PC, for the Zelux / Kiralux cameras.

    python scripts/setup_thorlabs_sdk.py <zip> [--dest FOLDER]

<zip> is EITHER the download "Windows SDK and Doc. for Scientific Cameras"
(scientific_camera_interfaces-*.zip, thorlabs.com > ThorCam page >
Programming Interfaces) OR the thorlabs_tsi_camera_python_sdk_package.zip
inside it (SDK\\Python Toolkit). The default FOLDER is camera-control\\vendor
(gitignored); the result is FOLDER\\thorlabs_tsi_sdk-<version> with the package and
its own DLLs (lib\\windows-x86_64). Not %LOCALAPPDATA%: for a program started from
the Claude desktop app Windows redirects it into the app's private storage, so
a service started from Mission Control would not find the copy (Blue Box,
2026-11-10).

WHY NOT pip: the SDK is not on PyPI, so it cannot be in pyproject.toml, and a
copy pip-installed into the camera's venv disappears at the next `uv sync`
(gotcha #29). A folder outside the venv survives every sync and every
"Rebuild Python environments". backends/thorlabs.py imports from it.

The camera's USB DRIVER is NOT in this zip: install ThorImageCAM (same page)
first. Nothing here touches a camera. Plain stdlib; runs with any Python 3.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import zipfile
from pathlib import Path, PurePosixPath

INNER = "thorlabs_tsi_camera_python_sdk_package.zip"


def default_dest() -> Path:
    """camera-control\\vendor: where backends/thorlabs.py looks first."""
    return Path(__file__).resolve().parents[1] / "vendor"


def python_package_zip(path: Path) -> zipfile.ZipFile:
    """The Python package zip: `path` itself, or the one inside the big SDK zip."""
    outer = zipfile.ZipFile(path)
    names = outer.namelist()
    if any(PurePosixPath(n).parts[:1] and PurePosixPath(n).parts[0].startswith("thorlabs_tsi_sdk-")
           for n in names):
        return outer
    inner = [n for n in names if n.replace("\\", "/").endswith("/" + INNER) or n == INNER]
    if not inner:
        raise SystemExit(f"{path.name}: neither the SDK download nor {INNER} "
                         "(no thorlabs_tsi_sdk-* folder and no Python Toolkit zip inside)")
    return zipfile.ZipFile(io.BytesIO(outer.read(inner[0])))


def unpack(zip_path: Path, dest: Path) -> Path:
    z = python_package_zip(zip_path)
    tops = {PurePosixPath(n).parts[0] for n in z.namelist() if n.strip("/")}
    pkg = sorted(t for t in tops if t.startswith("thorlabs_tsi_sdk-"))
    if len(pkg) != 1:
        raise SystemExit(f"expected one thorlabs_tsi_sdk-<version> folder, found {pkg}")
    root = (dest / pkg[0]).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for info in z.infolist():
        target = (dest / info.filename).resolve()
        # refuse "zip slip": every member must land inside dest
        if root != target and root not in target.parents:
            raise SystemExit(f"refusing an entry outside the package folder: {info.filename}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(z.read(info))
    if not (root / "thorlabs_tsi_sdk" / "tl_camera.py").is_file():
        raise SystemExit(f"{root}: no thorlabs_tsi_sdk/tl_camera.py after unpacking")
    return root


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("zip", type=Path, help="the SDK download or its Python package zip")
    ap.add_argument("--dest", type=Path, default=default_dest(),
                    help="folder to unpack into (default: camera-control\\vendor)")
    a = ap.parse_args(argv)
    if not a.zip.is_file():
        print(f"no such file: {a.zip}", file=sys.stderr)
        return 2
    root = unpack(a.zip, a.dest)
    bits = "windows-x86_64" if sys.maxsize > 2 ** 32 else "windows-x86"
    dlls = root / "lib" / bits
    print(f"thorlabs_tsi_sdk unpacked to {root}")
    print(f"  DLLs: {dlls} ({'present' if dlls.is_dir() else 'MISSING'})")
    if a.dest.resolve() != default_dest().resolve():
        print(f"  not the default folder: set [camera] thorlabs_sdk_dir = {root} in camera.ini")
    print("Next: install ThorImageCAM (the camera's USB driver), close it, then start the")
    print("camera service with driver = thorlabs (camera.ini [camera] driver = thorlabs).")
    print("If the SDK crashes on start (access violation): set [camera] thorlabs_dll_dir to")
    print("ThorCam's folder (C:\\Program Files\\Thorlabs\\Scientific Imaging\\ThorCam).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
