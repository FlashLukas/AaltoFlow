"""recording.py -- camera IMAGES as a scan detector (2026-10-10).

A scan can record one frame per point (scan-core's `camera.image` detector):
the full-depth frame as the sensor gives it (12-bit counts on the lab camera
in Mono12, the 8-bit frame on a camera that only delivers Mono8), optionally
cropped and binned so a 30 x 30 map does not become 20 GB.

Why a module of its own: the brain's frame loop (camera.py) is busy enough.
The brain calls three hooks -- `on_frame` right after every grab, `fill_status`
when it builds the status snapshot, `restore_auto` at shutdown -- and the
service the verbs. Everything else lives here.

THE FRESH-FRAME RULE (gotchas #17 and #28, as for every slow detector):
acquisitions are NUMBERED. `acquire()` returns the next number at once and
remembers WHEN it was asked; the engine thread latches the first frame whose
grab STARTED after that moment (plus `record_discard_frames` more, for a
camera that hands out frames already waiting in its buffer queue), and sets
the sample and "not busy" in ONE critical section. A scan waits for
"image_id == my number and not image_acquiring", then fetches the sample --
so a point never gets the frame of the point before.

WHICH PIXELS (cfg.image.record_*):
  * "full" -- the whole processed frame (after clip / rotation / mirror, like
    everything else this module shows);
  * "spot" -- a W x H crop CENTRED ON THE CALIBRATED LASER SPOT (the laser is
    fixed in the image, so the crop follows the calibration, not the sample).
    Shifted, never shrunk, at the frame's edge: every frame of a scan has the
    same shape. Refused while the spot is not calibrated.
  * "rect" -- a fixed W x H rectangle at (x, y).
  Binning 1, 2 or 4 SUMS b x b pixels (a photon count stays a photon count;
  a 4 x 4 bin of 12-bit counts reaches 65520 and still fits 16 bits).

AUTO EXPOSURE / AUTO GAIN (decision 2026-10-10): a map whose brightness the
camera re-adjusts from frame to frame is not a measurement. So when a frame is
asked for while the camera's ExposureAuto / GainAuto is not "Off", it is
switched Off (the camera keeps the value it had converged to) and RESTORED
when the scan that asked ends (its claim on the camera is released or lapses),
or -- for a client that never claimed the camera -- after
`record_auto_restore_s` without another image request, and at shutdown. The
camera ends as it was found (the adopt rule). Every switch is said in the log,
and the exposure and gain each frame was taken with travel with it.
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np

#: the camera features switched Off while images are recorded
AUTO_FEATURES = ("ExposureAuto", "GainAuto")

#: the recording ROI modes (cfg.image.record_roi) and binning factors
#: (cfg.image.record_binning) -- one list, in config.py
from .config import RECORD_BINNINGS as BINNINGS, RECORD_ROIS  # noqa: E402


class ImageRecorder:
    """Numbered image acquisitions for one Camera brain (see the module text)."""

    def __init__(self, brain):
        self.b = brain
        self._lock = threading.Lock()
        self._id = 0                    # last requested acquisition
        self._busy = False
        self._req: dict | None = None   # {"id", "t", "discard"} while waiting
        self._sample = None             # (meta, frame) of the last one taken
        self._error = ""                # why the last acquisition failed
        self._bits = 8                  # depth of the last frame seen
        self._frame_hw: tuple | None = None
        self._frozen: dict = {}         # feature -> its value before we switched it
        self._frozen_in_scan = False
        self._last_t = float("-inf")    # monotonic time of the last request

    # ------------------------------------------------------------------ #
    # geometry: which pixels, how big
    # ------------------------------------------------------------------ #
    def frame_hw(self) -> tuple:
        """(height, width) of the processed frame (the last one seen)."""
        if self._frame_hw is not None:
            return self._frame_hw
        frame = getattr(self.b, "_last_frame", None)
        if frame is not None:
            return tuple(frame.shape[:2])
        return (480, 640)

    def binning(self) -> int:
        b = int(getattr(self.b.cfg.image, "record_binning", 1) or 1)
        return b if b in BINNINGS else 1

    def box_size(self) -> tuple:
        """(h, w) of the region cut out of the frame, before binning --
        independent of WHERE it is, so describe can say it even before the
        spot is calibrated. A multiple of the binning (the rest is dropped)."""
        img = self.b.cfg.image
        fh, fw = self.frame_hw()
        b = self.binning()
        roi = img.record_roi if img.record_roi in RECORD_ROIS else "full"
        if roi == "full":
            h, w = fh, fw
        else:
            h = min(max(1, int(img.record_h)), fh)
            w = min(max(1, int(img.record_w)), fw)
        h, w = max(b, (h // b) * b), max(b, (w // b) * b)
        return min(h, (fh // b) * b), min(w, (fw // b) * b)

    def out_shape(self) -> tuple:
        """(h, w) of the frame a scan stores: the region after binning."""
        h, w = self.box_size()
        b = self.binning()
        return h // b, w // b

    def max_value(self, bits: int | None = None) -> int:
        """The largest value a stored pixel can take: full scale x b^2."""
        bits = int(bits or self._bits)
        b = self.binning()
        return ((1 << bits) - 1) * b * b

    def origin(self) -> tuple:
        """(y0, x0) of the region in the processed frame, or ValueError."""
        img = self.b.cfg.image
        fh, fw = self.frame_hw()
        h, w = self.box_size()
        roi = img.record_roi if img.record_roi in RECORD_ROIS else "full"
        if roi == "full":
            y0, x0 = 0, 0
        elif roi == "rect":
            y0, x0 = int(img.record_y), int(img.record_x)
        else:
            sp = self.b.cfg.spot
            if not sp.ref_set:
                raise ValueError(
                    "the recording region 'spot' is centred on the CALIBRATED laser "
                    "spot, and the spot is not calibrated (Spot tab: Calibrate spot) "
                    "-- or choose the region 'full' or 'rect' (Camera settings)")
            y0 = int(round(float(sp.ref_y) - h / 2.0))
            x0 = int(round(float(sp.ref_x) - w / 2.0))
        # shifted to stay inside the frame, never shrunk: one shape per scan
        y0 = min(max(0, y0), max(0, fh - h))
        x0 = min(max(0, x0), max(0, fw - w))
        return y0, x0

    def coords(self) -> dict:
        """The two image axes in px (centres of the stored pixels, in the
        processed frame) and in um (pixel size of the current objective)."""
        try:
            y0, x0 = self.origin()
        except ValueError:
            y0, x0 = 0, 0                 # the shape is right; the position follows
        h, w = self.out_shape()
        b = self.binning()
        # the centre of a b x b bin starting at pixel k is k + (b - 1) / 2
        y = y0 + b * np.arange(h) + (b - 1) / 2.0
        x = x0 + b * np.arange(w) + (b - 1) / 2.0
        px_x = float(self.b.cfg.image.pixel_size_x_um)
        px_y = float(self.b.cfg.image.pixel_size_y_um)
        return {"y": y.tolist(), "x": x.tolist(),
                "y_um": (y * px_y).tolist(), "x_um": (x * px_x).tolist(),
                "um_per_px": [px_x, px_y], "binning": b,
                "roi": [int(x0), int(y0), int(w * b), int(h * b)]}

    def wire_dtype(self, bits: int | None = None) -> np.dtype:
        top = self.max_value(bits)
        return np.dtype("|u1" if top <= 0xFF else "<u2" if top <= 0xFFFF else "<u4")

    def cut(self, src: np.ndarray, bits: int) -> np.ndarray:
        """The region of ``src`` (one processed frame), binned by summing."""
        y0, x0 = self.origin()
        h, w = self.box_size()
        b = self.binning()
        a = np.asarray(src)[y0:y0 + h, x0:x0 + w]
        if a.shape != (h, w):
            raise ValueError(f"the frame ({src.shape[1]} x {src.shape[0]}) is smaller "
                             f"than the recording region ({w} x {h})")
        if b > 1:
            # a FLOAT frame (a temporal average) would be summed as float; we
            # only ever get raw counts here, so integer sums are exact
            a = a.astype(np.int64).reshape(h // b, b, w // b, b).sum(axis=(1, 3))
        return np.ascontiguousarray(a).astype(self.wire_dtype(bits))

    # ------------------------------------------------------------------ #
    # the engine thread's hook
    # ------------------------------------------------------------------ #
    def on_frame(self, t_grab: float, gray, deep) -> None:
        """Called right after every grab of the frame loop (engine thread).

        ``t_grab`` = time.monotonic() just BEFORE the grab was asked for;
        ``deep`` = the brain's (8-bit frame, full-depth frame, bits) or None.
        Latches the frame for a waiting acquisition when it is fresh enough.
        """
        self._frame_hw = tuple(gray.shape[:2])
        if deep is not None and deep[0] is gray:
            src, bits = deep[1], int(deep[2])
        else:
            src, bits = gray, 8
        self._bits = bits
        with self._lock:
            req = self._req
        if req is None or t_grab < req["t"]:
            return                        # nothing asked, or exposed before the ask
        if req["discard"] > 0:
            req["discard"] -= 1           # a frame that may have waited in a queue
            return
        meta = {"image_id": req["id"], "bits": bits, "t": time.time()}
        try:
            frame = self.cut(src, bits)
            c = self.coords()
            meta.update(roi=c["roi"], binning=c["binning"],
                        exposure_us=self._feature_float("ExposureTime"),
                        gain=self._feature_float("Gain"))
            error = ""
        except Exception as exc:
            frame, error = None, f"image #{req['id']} not taken: {exc}"
        with self._lock:
            if self._req is not req:
                return                    # superseded by a newer request meanwhile
            # ONE critical section (gotcha #28): the sample and "not busy"
            # together, so no status frame says "done" with the old sample
            self._sample = None if frame is None else (meta, frame)
            self._error = error
            self._req = None
            self._busy = False

    def _feature_float(self, name: str) -> float:
        try:
            v = self.b.backend.get_feature(name)
            return float(v) if v is not None else float("nan")
        except Exception:
            return float("nan")

    # ------------------------------------------------------------------ #
    # the verbs
    # ------------------------------------------------------------------ #
    def acquire(self, in_scan: bool = False) -> int:
        """Ask for a fresh frame; returns its number at once (fire-and-forget)."""
        self.origin()                     # a region that cannot be cut: refuse NOW
        self._freeze_auto(in_scan)
        with self._lock:
            self._id += 1
            # the moment AFTER the auto features are off: an earlier frame was
            # exposed under the old settings and must not be the one taken
            self._req = {"id": self._id, "t": time.monotonic(),
                         "discard": max(0, int(self.b.cfg.image.record_discard_frames))}
            self._busy = True
            self._error = ""
            self._last_t = time.monotonic()
            return self._id

    def get(self, which: str = "sample") -> tuple:
        """(meta, frame): the last acquired frame ("sample"), or the region of
        the frame the loop holds now ("live", for a look -- not fresh)."""
        if which == "live":
            d = getattr(self.b, "_deep", None)
            gray = getattr(self.b, "_last_frame", None)
            if gray is None:
                raise RuntimeError("no frame yet")
            src, bits = ((d[1], int(d[2])) if d is not None and d[0] is gray
                         else (gray, 8))
            if src.dtype.kind == "f":     # a temporal average: no raw counts
                src, bits = gray, 8
            c = self.coords()
            return ({"image_id": None, "bits": bits, "t": time.time(),
                     "roi": c["roi"], "binning": c["binning"]}, self.cut(src, bits))
        with self._lock:
            busy, sample, err, n = self._busy, self._sample, self._error, self._id
        if busy:
            raise RuntimeError(f"image #{n} is still being taken")
        if sample is None:
            raise RuntimeError(err or "no image taken yet (send acquire_image first)")
        return sample

    # ------------------------------------------------------------------ #
    # auto exposure / gain
    # ------------------------------------------------------------------ #
    def _feature_names(self) -> set:
        try:
            return {f.get("name") for f in (self.b.backend.features() or [])}
        except Exception:
            return set()

    def _freeze_auto(self, in_scan: bool) -> None:
        names = self._feature_names()
        for name in AUTO_FEATURES:
            if name not in names:
                continue
            try:
                cur = self.b.backend.get_feature(name)
            except Exception:
                continue
            if cur is None or str(cur) == "Off":
                continue
            try:
                self.b.backend.set_feature(name, "Off")
            except Exception as exc:
                raise RuntimeError(
                    f"the camera's {name} is {cur!r} and could not be switched Off "
                    f"({exc}); a map whose brightness the camera keeps adjusting is "
                    f"not a measurement -- switch it Off in Camera settings") from None
            self._frozen.setdefault(name, cur)
            self.b._emit("info", f"recording images: {name} {cur} -> Off (the camera "
                                 f"keeps its present value; restored afterwards)")
        if self._frozen and in_scan:
            self._frozen_in_scan = True

    def restore_auto(self, why: str) -> None:
        """Put every auto feature we switched Off back as it was."""
        for name, old in list(self._frozen.items()):
            try:
                self.b.backend.set_feature(name, old)
                self.b._emit("info", f"recording images: {name} restored to {old} ({why})")
            except Exception as exc:
                self.b._emit("error", f"recording images: could NOT restore {name} to "
                                      f"{old} ({exc}) -- set it by hand (Camera settings)")
        self._frozen.clear()
        self._frozen_in_scan = False

    def housekeeping(self, scan_active: bool) -> None:
        """Restore the auto features once nobody records images any more.

        Called by the service a few times a second. Switched under a SCAN's
        claim: restored when that claim ends. Switched by a client without a
        claim: restored after record_auto_restore_s without another request.
        """
        if not self._frozen:
            return
        if self._frozen_in_scan:
            if not scan_active:
                self.restore_auto("the scan recording images ended")
            return
        idle = time.monotonic() - self._last_t
        limit = max(1.0, float(self.b.cfg.image.record_auto_restore_s))
        if idle > limit:
            self.restore_auto(f"no image asked for in {limit:g} s")

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #
    def fill_status(self, st) -> None:
        """Copy the recorder's state into a status snapshot (gotcha #1: the
        snapshot is rebuilt every frame; the truth lives here)."""
        with self._lock:
            st.image_id = self._id
            st.image_acquiring = self._busy
            sample = self._sample
            st.image_error = self._error
        st.image_sample_id = int(sample[0]["image_id"]) if sample else 0
        st.image_exposure_us = float(sample[0].get("exposure_us", math.nan)) if sample else math.nan
        st.image_gain = float(sample[0].get("gain", math.nan)) if sample else math.nan
        st.image_bits = int(self._bits)
        st.image_shape = list(self.out_shape())
        st.image_auto_frozen = ", ".join(f"{k} (was {v})" for k, v in list(self._frozen.items()))
