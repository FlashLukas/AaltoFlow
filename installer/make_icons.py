r"""make_icons.py -- render the suite's icon.svg files into Windows .ico files.

Run by build_installer.ps1 against the staged copy:

    python make_icons.py <stage-dir> <out-dir>

writes  <out-dir>\<folder>.ico  for every <folder>\icon.svg in the stage.

Why: Windows shortcuts and Setup.exe want .ico, and Explorer picks a different
size depending on where it draws them (16 px in a list, 256 px on a large
tile), so each .ico carries every size. The SVGs are the ones the launcher
already draws on its cards, so the Start menu ends up showing the same picture
as the dashboard -- one source, no second set of drawings to keep in step.

Rendered with Qt's SVG renderer, the same one the launcher uses, so what you
see in the Start menu is what the card shows. The colours are the DARK palette
(grey chassis, amber accent); they sit fine on both a light and a dark Start
menu, which is why nothing is re-themed here.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Sizes Windows asks for: list view, small tile, large tile, and the 256 px
# one Explorer scales down when a display is at high DPI.
SIZES = [16, 24, 32, 48, 64, 128, 256]


def render(svg: Path, out: Path) -> None:
    from PySide6 import QtCore, QtGui, QtSvg
    from PIL import Image

    data = QtCore.QByteArray(svg.read_bytes())
    renderer = QtSvg.QSvgRenderer(data)
    if not renderer.isValid():
        raise ValueError(f"{svg} is not a renderable SVG")

    frames = []
    for size in SIZES:
        image = QtGui.QImage(size, size, QtGui.QImage.Format_RGBA8888)
        image.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(image)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()
        # QImage -> Pillow without a temp file: constBits() is the raw RGBA.
        buf = image.constBits().tobytes()
        frames.append(Image.frombytes("RGBA", (size, size), buf))

    # Pillow writes one .ico holding every size from the largest frame.
    frames[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES])


def main() -> int:
    stage = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # Qt needs an application object before it will paint, even offscreen.
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtGui
    app = QtGui.QGuiApplication([])  # noqa: F841  (kept alive for the renders)

    made = 0
    for svg in sorted(stage.glob("*/icon.svg")):
        target = out / f"{svg.parent.name}.ico"
        try:
            render(svg, target)
        except Exception as exc:                      # noqa: BLE001
            print(f"  SKIPPED {svg.parent.name}: {exc}")
            continue
        made += 1
    print(f"  {made} icons -> {out}")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
