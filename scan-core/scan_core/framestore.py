"""framestore.py -- IMAGE detectors: one camera frame per scan point (2026-10-10).

A camera (or any detector that returns a 2-D array) is an array detector like
a VNA trace, only much bigger: a 1936 x 1096 frame is 2.1 million numbers, a
30 x 30 map of them 1.9 billion. Three things follow, and this file holds the
rules for them.

1. WHAT COUNTS AS AN IMAGE: an array detector with two or more inner axes
   (`is_image`). Its numbers are held in memory as float32 when they are
   whole counts that fit 24 bits (storage.Storage.allocate(compact=True)),
   it gets a per-point mask `<det>_measured` (1 = a frame was taken here),
   and it is written in chunks of ONE FRAME each, so reading one point's
   frame back never decompresses the whole map.

2. HOW BIG IT WILL BE, before the scan starts (`estimate`, `format_bytes`):
   points x pixels x bytes per stored pixel, uncompressed -- the Scan tab
   shows it ("~2.1 GB") and the engine decides with it.

3. WRITE AS YOU GO (the cheap half of roadmap phase 2). An image variable
   above INCREMENTAL_ABOVE_BYTES is NOT held in memory at all. Its frames go
   straight into the scan's data file as they arrive (`FrameBuffer`): the
   file is created at the first frame -- every small variable, coordinate
   and attribute written by xarray as usual, then the image variable added,
   pre-allocated, chunked per frame and compressed -- and each later frame is
   written into its slot. Unwritten chunks are not stored at all; a reader
   sees them as the fill value, i.e. "not measured". The rest of the scan
   (the small variables and the attributes: run info, snapshot, `seconds`)
   is written INTO THE SAME FILE in place at every checkpoint and at the
   end (`write_with_frames`), instead of the usual "write a temporary file
   and rename it" -- copying gigabytes at every checkpoint is what this
   avoids. So a crash keeps every frame taken so far.

   What it costs, and why it is acceptable: the dataset the engine hands
   around (live plot, `run()`'s return value) does NOT contain the big
   image variable -- only its mask and coordinates; the frames are in the
   file (`ds.encoding["aaltoflow_frames"]` names the buffer, and the live
   view takes the latest frame from `latest_frames(ds)`). And such a scan
   needs a data file: without one it is refused, with the estimate in the
   message.

The in-file layout is the SAME on both paths, so AaltoView (and anybody with
xarray, MATLAB, h5py) reads a big map exactly like a small one.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import numpy as np

from .storage import COMPRESSION, storage_of

#: An image variable estimated (uncompressed, as stored) above this many bytes
#: is written to the file frame by frame instead of being held in memory.
#: 1 GB: in memory the frames are float32 (2 bytes per 12-bit pixel become 4),
#: so 2 GB of RAM plus a copy for the CF encoding at every checkpoint -- about
#: what a lab PC can spare next to the GUI. Tests lower it.
INCREMENTAL_ABOVE_BYTES = 1_000_000_000

#: where a dataset carries its frame buffers ({det: FrameBuffer}); in memory
#: only -- xarray never writes Dataset.encoding keys it does not know
ENCODING_KEY = "aaltoflow_frames"

#: ... and the latest frame of every image detector ({det: (index, frame)}),
#: for the live view (both paths)
LATEST_KEY = "aaltoflow_latest"

#: the attribute that marks an image variable in the file (1)
IMAGE_ATTR = "aaltoflow_image"


def is_image(g) -> bool:
    """True for an array detector with two or more inner axes (a frame)."""
    return g is not None and len(getattr(g, "axes", ()) or ()) >= 2


def frame_shape(g, fetch: bool = False) -> tuple | None:
    """The frame's shape from the detector's axes. Uses each axis's declared
    `length` (describe's dims, no network); `fetch=True` asks the module for
    the coordinates where a length is missing. None when unknown."""
    out = []
    for ax in getattr(g, "axes", ()) or ():
        n = getattr(ax, "length", None)
        if not n:
            if not fetch:
                return None
            n = len(ax.values())
        out.append(int(n))
    return tuple(out)


def bytes_per_value(g) -> int:
    """How many bytes one stored value of detector `g` takes on disk."""
    st = storage_of(g)
    if st.kind == "complex":
        return 2 * st.disk.itemsize
    if st.kind == "string":
        return 8
    return int(st.disk.itemsize)


def image_bytes(g, n_points: int, fetch: bool = False) -> int | None:
    """Uncompressed size of detector `g`'s variable over `n_points` points."""
    shape = frame_shape(g, fetch=fetch)
    if shape is None:
        return None
    return int(n_points) * int(np.prod(shape, dtype=np.int64)) * bytes_per_value(g)


def estimate(recipe, registry) -> dict:
    """{detector id: bytes or None} for every IMAGE detector the recipe records.

    Uncompressed, as stored: compression usually takes a camera image to
    30-70 % of this (noise compresses badly), so this is the honest upper
    end. No network: a frame whose shape the module did not declare (no
    `length` in its dims) gives None. Repeats are counted as stored (an
    averaged repeat stores one frame, not N)."""
    out = {}
    dets = list(getattr(recipe, "detectors", None) or [])
    imgs = [d for d in dets if is_image(registry.get(d))]
    if not imgs:
        return out
    try:
        comp = recipe.compile(registry)
        n = int(comp.n_points)
        from .repeat import average_index
        avg = average_index(comp.dims)
        if avg is not None:
            n //= max(1, int(comp.dims[avg].size))
    except Exception:
        return {d: None for d in imgs}
    for d in imgs:
        out[d] = image_bytes(registry.get(d), n)
    return out


def format_bytes(n) -> str:
    """'~2.1 GB', '~350 MB', '~12 kB' (decimal units, as disks are sold)."""
    if n is None:
        return "size unknown"
    n = float(n)
    for unit, scale in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            v = n / scale
            return f"~{v:.1f} {unit}" if v < 100 else f"~{v:.0f} {unit}"
    return f"~{int(n)} B"


def mask_name(det: str) -> str:
    """The per-point mask variable of image detector `det`."""
    return f"{det}_measured"


def chunks_for(scan_shape, frame) -> tuple:
    """One frame per chunk: every scan dim 1, the frame's dims whole."""
    return tuple(1 for _ in scan_shape) + tuple(int(n) for n in frame)


# ─────────────────────────── write as you go ────────────────────────────────

class FrameBuffer:
    """Stands in for the engine's in-memory buffer of ONE big image detector.

    The engine does `buf[idx] = frame` (and, for a skip_if after the point,
    `old = buf[idx]` / `buf[idx] = old`) exactly as with a numpy array; this
    writes into the data file instead. Opened and closed per frame (a few
    milliseconds): no handle stays open between frames, so the checkpoint
    writes (xarray, mode "a") and a later "Save data as" can open the file.

    `skeleton_fn()` builds the dataset as it stands (the engine's
    `_to_dataset`), used once to create the file; building it also fills in
    `dims` and `attrs` of this variable (the engine sets them on the buffer).
    """

    def __init__(self, path, name: str, scan_shape, frame, storage, skeleton_fn):
        self.path = Path(path)
        self.name = name
        self.scan_shape = tuple(int(n) for n in scan_shape)
        self.frame = tuple(int(n) for n in frame)
        self.shape = self.scan_shape + self.frame
        self.ndim = len(self.shape)
        self.storage = storage
        self.skeleton_fn = skeleton_fn
        self.dims: list[str] | None = None        # set by the engine's _to_dataset
        self.attrs: dict = {}
        self.dtype = np.dtype(np.float32)          # what __getitem__ hands back
        self._lock = threading.Lock()
        self.created = False
        self.n_written = 0

    # what the file holds per pixel, and the "not measured" value
    @property
    def disk(self) -> np.dtype:
        return self.storage.disk

    @property
    def fill(self):
        f = self.storage.fill
        return self.disk.type(f) if f is not None else np.float64(np.nan).astype(self.disk)

    def ensure_file(self, skeleton=None) -> None:
        """Create the data file with every small variable and this (empty)
        image variable, once. `skeleton` = a dataset to write (else
        skeleton_fn() is called)."""
        with self._lock:
            if self.created:
                return
            ds = skeleton if skeleton is not None else self.skeleton_fn()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # ONE writer creates the file: plain xarray for everything small,
            # exactly what every other scan writes
            ds.to_netcdf(self.path, engine="h5netcdf")
            self._create_variable()
            self.created = True

    def _create_variable(self) -> None:
        import h5netcdf
        if not self.dims:
            raise RuntimeError(f"{self.name}: dimension names unknown (engine bug)")
        comp = {"compression": "gzip", "compression_opts": COMPRESSION["complevel"],
                "shuffle": COMPRESSION["shuffle"]}
        with h5netcdf.File(self.path, "r+") as f:
            v = f.create_variable(self.name, tuple(self.dims), dtype=self.disk,
                                  chunks=chunks_for(self.scan_shape, self.frame),
                                  fillvalue=self.fill, **comp)
            for k, val in self.attrs.items():
                if k in ("_FillValue",):
                    continue
                v.attrs[k] = val

    def __setitem__(self, idx, value) -> None:
        if not self.created:
            self.ensure_file()
        arr = np.asarray(value, dtype=np.float64)
        if arr.shape != self.frame:
            raise ValueError(f"{self.name}: frame of shape {arr.shape}, the file "
                             f"was made for {self.frame}")
        nan = np.isnan(arr)
        out = np.where(nan, 0.0, arr).astype(self.disk)
        if nan.any():
            out[nan] = self.fill
        import h5netcdf
        with self._lock:
            with h5netcdf.File(self.path, "r+") as f:
                f[self.name][tuple(int(i) for i in idx)] = out
            self.n_written += 1

    def __getitem__(self, idx) -> np.ndarray:
        """The frame stored at `idx` as float32 with NaN = not measured."""
        if not self.created:
            return np.full(self.frame, np.nan, dtype=np.float32)
        import h5netcdf
        with self._lock:
            with h5netcdf.File(self.path, "r") as f:
                raw = np.asarray(f[self.name][tuple(int(i) for i in idx)])
        out = raw.astype(np.float32)
        out[raw == self.fill] = np.nan
        return out

    def copy(self):
        # live snapshots never copy an image buffer (see engine.live); a
        # caller that asks gets the buffer itself, which is read-only for it
        return self


def frames_of(ds) -> dict:
    """{det: FrameBuffer} a dataset's frames are written to ({} = none)."""
    enc = getattr(ds, "encoding", None) or {}
    return dict(enc.get(ENCODING_KEY) or {})


def latest_frames(ds) -> dict:
    """{det: (index, frame)} -- the newest frame of every image detector
    the engine knew about when it built `ds` ({} for a dataset read from a
    file: the live view then looks for the last measured frame itself)."""
    enc = getattr(ds, "encoding", None) or {}
    return dict(enc.get(LATEST_KEY) or {})


def _same_file(a, b) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:
        return False


def write_with_frames(ds, path) -> Path:
    """Write `ds` (the small variables) for a scan whose big frames are
    already in a file (frames_of(ds) is not empty).

    To the scan's own file: in place (xarray mode "a" overwrites the small
    variables and attributes; the frames stay where they are). Anywhere else
    ("Save data as"): the frame file is copied there first, then updated.
    """
    from suite_common.fileio import replace_retry
    path = Path(path)
    bufs = list(frames_of(ds).values())
    src = bufs[0].path
    for b in bufs:
        b.ensure_file(skeleton=ds)
    if _same_file(path, src):
        ds.to_netcdf(path, mode="a", engine="h5netcdf")
        return path
    tmp = path.with_suffix(".writing.nc")
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, tmp)
    ds.to_netcdf(tmp, mode="a", engine="h5netcdf")
    replace_retry(tmp, path)
    return path
