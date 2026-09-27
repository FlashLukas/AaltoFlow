"""Save / load the tracking template as an annotated PNG.

The LabVIEW program saved "the pattern for the feedback to a png file that also
contains a lot of meta data" -- the spot<->scanning-array distance, the scanning
array details, and so on -- and could load it back.  We do the same: the template
image is written as a normal grayscale PNG, and ALL the metadata travels with it
inside a PNG ``tEXt`` chunk (key ``TRMOKE_META``) as JSON.  So the file is both a
viewable picture AND a complete, self-describing reference you can reload later.

A :class:`Reference` bundles everything the brain needs to resume tracking:
  * ``template`` -- the grayscale patch to pattern-match each frame.
  * ``array_center_offset_px`` -- vector from the template match point to the
    centre of the scanning array (the 'Template-array distance' in LabVIEW).
  * ``meta`` -- scanning geometry, pixel size, objective, selected index, etc.
  * ``backups`` -- BACKUP patterns (2026-09-14): other features, each stored with
    its offset from the main template. When a scan pushes the main template off
    the screen, a backup still in view carries the tracking (see
    Camera._track_patterns). They travel in the same PNG, base64 inside the JSON.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, PngImagePlugin

META_KEY = "TRMOKE_META"


@dataclass
class BackupPattern:
    template: np.ndarray                       # grayscale uint8 patch
    offset_px: tuple = (0.0, 0.0)              # its centre minus the MAIN template's centre


@dataclass
class Reference:
    template: np.ndarray                       # grayscale uint8 patch
    array_center_offset_px: tuple = (0.0, 0.0)  # template -> array-centre (px)
    meta: dict = field(default_factory=dict)    # scan geometry, pixel size, ...
    backups: list = field(default_factory=list)  # [BackupPattern], in capture order

    def describe(self) -> str:
        h, w = self.template.shape[:2]
        return (f"template {w}x{h}px, array offset "
                f"({self.array_center_offset_px[0]:.1f}, "
                f"{self.array_center_offset_px[1]:.1f})px")


def save_template(path: str, ref: Reference) -> None:
    """Write ``ref`` to ``path`` as a PNG with embedded JSON metadata."""
    img = ref.template
    if img.ndim == 3:
        pil = Image.fromarray(img[..., ::-1])   # BGR -> RGB
    else:
        pil = Image.fromarray(img)

    meta = dict(ref.meta)
    meta["array_center_offset_px"] = list(ref.array_center_offset_px)
    meta["backups"] = [{"offset_px": [float(b.offset_px[0]), float(b.offset_px[1])],
                        "png_b64": _png_b64(b.template)} for b in ref.backups]
    meta["_schema"] = "TRMOKE-camera-template/1"

    info = PngImagePlugin.PngInfo()
    info.add_text(META_KEY, json.dumps(meta))
    pil.save(path, "PNG", pnginfo=info)


def load_template(path: str) -> Reference:
    """Load a template PNG written by :func:`save_template`.

    If the metadata chunk is missing (e.g. a plain PNG dropped in by hand) the
    image still loads with empty metadata and a zero array offset.
    """
    pil = Image.open(path)
    raw = pil.text.get(META_KEY) if hasattr(pil, "text") else None
    meta = {}
    if raw:
        try:
            meta = json.loads(raw)
        except Exception:
            meta = {}

    gray = np.array(pil.convert("L"))
    offset = tuple(meta.pop("array_center_offset_px", (0.0, 0.0)))
    meta.pop("_schema", None)
    backups = []
    for b in meta.pop("backups", None) or []:
        try:
            tpl = np.array(Image.open(io.BytesIO(base64.b64decode(b["png_b64"]))).convert("L"))
            backups.append(BackupPattern(tpl, tuple(float(v) for v in b["offset_px"])))
        except Exception:
            continue          # one unreadable backup must not lose the main pattern
    return Reference(template=gray, array_center_offset_px=offset, meta=meta,
                     backups=backups)


def _png_b64(gray: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(gray).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")
