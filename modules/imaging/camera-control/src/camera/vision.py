"""The vision engine -- PURE image processing (OpenCV + NumPy + SciPy).

No sockets, no threads, no hardware, no config objects: every function takes
plain arrays/numbers and returns plain results, so it is trivially unit-testable
and reusable.  The brain (camera.py) wires these into the live loop.

This module ports the maths of the LabVIEW sub-VIs:
  * FindSpotInImageandMarkit         -> find_spot()
  * (NI pattern match, Grayscale Pyramid) -> match_template()
  * AutofocusEdgesAndFFT             -> focus_metric()
  * ScanningArrayPixelDistancesCentered -> scanning_array_pixel_offsets()
  * OverlayScanningPointsWithCentered (distance maths) -> selected_point_distance()

Coordinate convention (OpenCV): x increases to the RIGHT, y increases DOWN.
Grayscale images are 2-D uint8 arrays.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

# For each focus mechanism, is the BEST focus the MAXIMUM of the metric?
# A laser spot is tightest (smallest area) at focus -> minimise.
# Edge sharpness and high-frequency FFT energy peak at focus -> maximise.
# spot_d4sigma (the spot's second moment sigma^2) and spot_relative (the area
# above a fraction of the spot's own peak) are sizes too -> minimise.
FOCUS_MAXIMISE = {"spot_area": False, "edges": True, "fft": True,
                  "spot_d4sigma": False, "spot_relative": False}


# --------------------------------------------------------------------------- #
# Frame preprocessing (clip -> rotate -> mirror)
# --------------------------------------------------------------------------- #
def to_gray(image: np.ndarray) -> np.ndarray:
    """Return a 2-D uint8 grayscale view of ``image`` (accepts colour too)."""
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.dtype != np.uint8:
        image = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return image


def preprocess(
    image: np.ndarray,
    rotation_deg: float = 0.0,
    symmetry: str = "none",
    clip: tuple[int, int, int, int] | None = None,
) -> np.ndarray:
    """Apply the standing geometry corrections to a raw frame.

    ``clip`` is (left, top, right, bottom); a right/bottom of 0 means "to the
    edge".  Rotation is about the image centre; ``symmetry`` mirrors after.
    """
    img = image
    if clip is not None:
        h, w = img.shape[:2]
        left, top, right, bottom = clip
        right = w if right <= 0 else min(right, w)
        bottom = h if bottom <= 0 else min(bottom, h)
        left = max(0, min(left, w - 1))
        top = max(0, min(top, h - 1))
        if right > left and bottom > top:
            img = img[top:bottom, left:right]
    if rotation_deg:
        h, w = img.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), rotation_deg, 1.0)
        img = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR)
    if symmetry == "horizontal":
        img = cv2.flip(img, 1)   # mirror left<->right
    elif symmetry == "vertical":
        img = cv2.flip(img, 0)   # mirror top<->bottom
    return img


# --------------------------------------------------------------------------- #
# Spot finding  (threshold -> largest blob -> centre of mass)
# --------------------------------------------------------------------------- #
@dataclass
class SpotReport:
    """Everything the LabVIEW 'Found spot' cluster reported, plus the essentials."""

    found: bool = False
    cx: float = 0.0          # centre of mass, pixels (x)
    cy: float = 0.0          # centre of mass, pixels (y)
    area: float = 0.0        # thresholded area, px^2
    bbox: tuple = (0, 0, 0, 0)   # x, y, w, h
    n_holes: int = 0
    orientation_deg: float = 0.0
    major: float = 0.0       # blob major/minor extent (px)
    minor: float = 0.0


def search_region(center, half_x: int, half_y: int = 0, shape: str = "rect",
                  frame_size=None) -> dict | None:
    """The spot search region around ``center``.

    "rect": +/- ``half_x`` px in x, +/- ``half_y`` px in y (0 = same as x).
    "circle": radius ``half_x`` px (``half_y`` ignored).
    Returns {"shape", "center", "half": (hx, hy), "box": (x0, y0, x1, y1)} with the
    bounding box clipped to ``frame_size`` = (w, h) when given; None if empty.
    """
    shape = "circle" if str(shape).lower().startswith("circ") else "rect"
    hx = int(half_x)
    hy = hx if (shape == "circle" or not half_y) else int(half_y)
    if hx <= 0 or hy <= 0:
        return None
    cx, cy = int(round(center[0])), int(round(center[1]))
    x0, y0, x1, y1 = cx - hx, cy - hy, cx + hx + 1, cy + hy + 1
    if frame_size is not None:
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(int(frame_size[0]), x1), min(int(frame_size[1]), y1)
    if x1 <= x0 or y1 <= y0:
        return None
    return {"shape": shape, "center": (float(center[0]), float(center[1])),
            "half": (hx, hy), "box": (x0, y0, x1, y1)}


def spot_candidates(stats, offset, full_size, min_area_px=4, max_area_px=0,
                    reject_border=False) -> np.ndarray:
    """Boolean per blob (``stats`` rows 1..n from connectedComponentsWithStats):
    may this blob be the spot? ``offset`` is where a cropped search box sits in
    the full frame, ``full_size`` = (w, h) of the full frame."""
    s = stats[1:]
    area = s[:, cv2.CC_STAT_AREA]
    ok = area >= max(1, int(min_area_px))
    if max_area_px and max_area_px > 0:
        ok &= area <= int(max_area_px)
    if reject_border:
        x0 = s[:, cv2.CC_STAT_LEFT] + offset[0]
        y0 = s[:, cv2.CC_STAT_TOP] + offset[1]
        x1 = x0 + s[:, cv2.CC_STAT_WIDTH]
        y1 = y0 + s[:, cv2.CC_STAT_HEIGHT]
        ok &= (x0 > 0) & (y0 > 0) & (x1 < full_size[0]) & (y1 < full_size[1])
    return ok


def find_spot(
    image: np.ndarray,
    thr_lower: int = 200,
    thr_upper: int = 255,
    bright_spot: bool = True,
    lookup_region_px: int = 0,
    last_xy: tuple[float, float] | None = None,
    min_area_px: int = 4,
    max_area_px: int = 0,
    reject_border: bool = False,
    search_shape: str = "rect",
    lookup_region_y_px: int = 0,
    symmetric: bool = False,
) -> SpotReport:
    """Locate the laser spot by intensity threshold + centre of mass.

    If ``lookup_region_px`` > 0 and ``last_xy`` is given, only a region around
    that point is searched (speed / robustness), see :func:`search_region`:
    ``search_shape`` "rect" = +/- ``lookup_region_px`` in x and
    +/- ``lookup_region_y_px`` in y (0 = same as x), "circle" = radius
    ``lookup_region_px``. Coordinates are still returned in FULL-frame pixels.

    Candidates are the thresholded blobs with ``min_area_px <= area`` and, when
    given, ``area <= max_area_px``; with ``reject_border`` a blob touching the
    edge of the FULL frame is not a candidate either. The largest candidate wins.
    Why the two filters (lab rig, 63x, 2026-09-14): saturated illumination
    filling a corner of the frame was a 396 000 px blob touching the border, the
    real laser spot a 1 300 px blob in the middle -- and "largest blob" picked
    the corner, so the calibrated spot position was the centre of that corner.
    """
    gray = to_gray(image)
    full_h, full_w = gray.shape[:2]
    ox, oy = 0, 0
    region = None
    if lookup_region_px > 0 and last_xy is not None:
        region = search_region(last_xy, lookup_region_px, lookup_region_y_px, search_shape,
                               (full_w, full_h))
        if region is not None:
            ox, oy, x1, y1 = region["box"]
            gray = gray[oy:y1, ox:x1]

    # Build the binary mask of "spot" pixels.
    if not bright_spot:
        gray = cv2.bitwise_not(gray)
        thr_lower, thr_upper = 255 - thr_upper, 255 - thr_lower
    mask = cv2.inRange(gray, int(thr_lower), int(thr_upper))
    if region is not None and region["shape"] == "circle":
        # only the disc counts: blank the corners of the bounding box
        disc = np.zeros_like(mask)
        cv2.circle(disc, (int(round(last_xy[0])) - ox, int(round(last_xy[1])) - oy),
                   int(lookup_region_px), 255, -1)
        mask = cv2.bitwise_and(mask, disc)

    # SYMMETRIC about the calibrated centre (Lukáš, 2026-09-14): when the search
    # region reaches another bright object, that object must not count as spot
    # -- neither as a bigger separate blob, nor merged onto the spot's edge
    # (which inflates the area and drags the centroid). The laser spot is
    # symmetric about its centre, an intruder is not: keep only the pixels whose
    # mirror image through the calibrated centre is ALSO bright, then take the
    # blob that is centred there.
    center_rc = None
    unsym = None
    if symmetric and region is not None and last_xy is not None:
        hx, hy = region["half"]
        ccx, ccy = int(round(last_xy[0])), int(round(last_xy[1]))
        cx0, cy0 = ccx - hx, ccy - hy                  # canvas origin, full frame
        canvas = np.zeros((2 * hy + 1, 2 * hx + 1), np.uint8)
        canvas[oy - cy0:oy - cy0 + mask.shape[0], ox - cx0:ox - cx0 + mask.shape[1]] = mask
        unsym = canvas
        mask = cv2.bitwise_and(canvas, canvas[::-1, ::-1])
        ox, oy = cx0, cy0
        center_rc = (hx, hy)
    if mask.sum() == 0:
        return SpotReport(found=False)

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return SpotReport(found=False)
    ok = spot_candidates(stats, (ox, oy), (full_w, full_h), min_area_px, max_area_px,
                         reject_border)
    if not ok.any():
        return SpotReport(found=False)
    if center_rc is not None:
        # the candidate CENTRED on the calibrated position (a symmetric blob's
        # centroid is the centre of symmetry; mirror pairs of blobs are not)
        d = np.hypot(centroids[1:, 0] - center_rc[0], centroids[1:, 1] - center_rc[1])
        d = np.where(ok, d, np.inf)
        j = int(np.argmin(d))
        if not d[j] <= 1.5:
            return SpotReport(found=False)
        idx = 1 + j
    else:
        # Largest CANDIDATE connected component = the spot.
        areas = np.where(ok, stats[1:, cv2.CC_STAT_AREA], -1)
        idx = 1 + int(np.argmax(areas))
    area = float(stats[idx, cv2.CC_STAT_AREA])

    blob = (labels == idx).astype(np.uint8)
    # Sub-pixel centre of mass from the blob's moments.
    m = cv2.moments(blob, binaryImage=True)
    cx = m["m10"] / m["m00"]
    cy = m["m01"] / m["m00"]
    if unsym is not None:
        # The symmetric blob is centred on the calibration BY CONSTRUCTION, so
        # its centroid says nothing about drift. The live centroid comes from
        # the raw bright pixels within a disc just larger than the symmetric
        # blob (its extent + 3 px): a spot that has drifted a little shows up
        # there, while an object touching the spot adds at most that thin rim.
        # (An intruder merged onto the spot and a spot drifting are the same
        # thing to a centroid -- this is information only, never used to move.)
        ys, xs = np.nonzero(blob)
        r = float(np.sqrt(((xs - cx) ** 2 + (ys - cy) ** 2).max())) + 3.0
        yy, xx = np.ogrid[:unsym.shape[0], :unsym.shape[1]]
        near = ((xx - cx) ** 2 + (yy - cy) ** 2 <= r * r) & (unsym > 0)
        if near.any():
            ny, nx = np.nonzero(near)
            cx, cy = float(nx.mean()), float(ny.mean())

    # Orientation + extent from central moments.
    mu20 = m["mu20"] / m["m00"]
    mu02 = m["mu02"] / m["m00"]
    mu11 = m["mu11"] / m["m00"]
    theta = 0.5 * math.atan2(2 * mu11, (mu20 - mu02))
    common = math.sqrt(max(0.0, (mu20 - mu02) ** 2 + 4 * mu11 ** 2))
    major = math.sqrt(max(0.0, 2 * (mu20 + mu02 + common)))
    minor = math.sqrt(max(0.0, 2 * (mu20 + mu02 - common)))

    x, y, w_, h_ = (
        int(stats[idx, cv2.CC_STAT_LEFT]),
        int(stats[idx, cv2.CC_STAT_TOP]),
        int(stats[idx, cv2.CC_STAT_WIDTH]),
        int(stats[idx, cv2.CC_STAT_HEIGHT]),
    )

    # Count holes (internal contours) inside the blob.
    contours, hierarchy = cv2.findContours(blob, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    n_holes = 0
    if hierarchy is not None:
        n_holes = int(sum(1 for hrow in hierarchy[0] if hrow[3] != -1))

    return SpotReport(
        found=True,
        cx=cx + ox,
        cy=cy + oy,
        area=area,
        bbox=(x + ox, y + oy, w_, h_),
        n_holes=n_holes,
        orientation_deg=math.degrees(theta),
        major=major,
        minor=minor,
    )


def spot_center_of_mass(image, thr_lower=200, thr_upper=255, bright_spot=True):
    """Convenience: just the (cx, cy) or None."""
    r = find_spot(image, thr_lower, thr_upper, bright_spot)
    return (r.cx, r.cy) if r.found else None


# --------------------------------------------------------------------------- #
# Spot SIZE without a fixed threshold (2026-09-28)
# --------------------------------------------------------------------------- #
# Why (Lukas, 2026-09-28): a fixed threshold measures a defocused spot wrongly.
# Defocus spreads the same power over a bigger area, the peak drops below the
# threshold and the thresholded area SHRINKS or vanishes -- so far from focus a
# blurred spot can look "small". Worse, the laser is highly coherent: a
# defocused spot has RINGS and can have a dark HOLE in its centre. That is a
# real feature of the spot, not noise, so no smoothing removes it, and every
# threshold (fixed, half maximum, Otsu) cuts it in a z-dependent way.
#
# The cure is the SECOND MOMENT of the intensity (ISO 11146, "D4sigma"):
#     sigma_x^2 = sum(I (x - xc)^2) / sum(I),   xc = sum(I x) / sum(I)
# It weighs every photon by its squared distance from the intensity centroid,
# with no threshold at all, so a ring or a hole is simply counted where it is.
# For ANY coherent paraxial beam (Gaussian or not) it obeys EXACTLY
#     sigma^2(z) = sigma0^2 + c (z - z0)^2
# -- a parabola in z. So an autofocus that fits a parabola to sigma^2 is fitting
# the right curve everywhere, not only near the bottom. The first moment (the
# centroid) of a donut is the centre of its hole, as it should be.
#
# What makes it work on a camera (ISO 11146-3 practice, plus what the
# coherent simulator taught us on 2026-09-28):
#   * subtract the BACKGROUND (robust mean of a ring just outside the box) and
#     set to zero what is not significantly above it -- far from the spot a
#     pixel's r^2 is huge, so noise there would dominate the sum. BUT the
#     decision "significant" must be made on a lightly blurred copy, and
#     generously: the textbook per-pixel 3-sigma clip deletes the faint wings
#     that carry most of r^2 and biased the focus by ~0.7 Rayleigh ranges in
#     the simulator (details in spot_second_moment);
#   * integrate over a box ~3 x D4sigma wide centred on the centroid, and
#     ITERATE (box -> centroid + width -> new box) until the width stops
#     changing -- again because the far wings dominate r^2;
#   * no saturated pixels at focus: a clipped peak loses power exactly where r
#     is small, which inflates sigma^2 (we FLAG saturation, we cannot undo it);
#   * dynamic range: out of focus the wings sit at ~1 % of the peak. On an
#     8-bit camera that is below one grey level, and sigma^2 comes out too
#     small there -- a 10/12-bit pixel format helps (camera "PixelFormat").
# Optional smoothing (smooth_px) adds exactly smooth_px^2 to sigma^2, which is
# subtracted again, so it cannot move the vertex.


@dataclass
class SpotMoments:
    """The spot's intensity moments (ISO 11146 style). Pixels, full-frame coords."""

    ok: bool = False
    why: str = ""                # when not ok: why nothing was measured
    cx: float = float("nan")     # intensity centroid (first moment)
    cy: float = float("nan")
    sigma2_x: float = float("nan")   # second central moments, px^2
    sigma2_y: float = float("nan")
    sigma2_xy: float = float("nan")
    sigma2: float = float("nan")     # mean of sigma2_x and sigma2_y
    d4sigma: float = float("nan")    # 4 sqrt(sigma2): the ISO beam DIAMETER, px
    d4sigma_x: float = float("nan")
    d4sigma_y: float = float("nan")
    peak: float = 0.0            # brightest pixel above background, counts
    background: float = 0.0      # robust mean of the ring outside the box, counts
    noise: float = 0.0           # its robust standard deviation, counts
    total: float = 0.0           # summed signal above background (counts)
    saturated: bool = False      # a pixel inside the box at the camera's maximum
    n_iter: int = 0
    converged: bool = False
    box: tuple = (0, 0, 0, 0)    # integration box used, x0, y0, x1, y1 (x1/y1 exclusive)
    clipped: bool = False        # the box wanted to be bigger than the search region


@dataclass
class SpotRelArea:
    """Area above a fraction of the spot's OWN peak (e.g. 1/e^2 = 13.5 %)."""

    ok: bool = False
    why: str = ""
    area: float = float("nan")   # px^2
    cx: float = float("nan")     # centroid of the selected pixels (unweighted)
    cy: float = float("nan")
    peak: float = 0.0            # brightest (3x3-averaged) pixel above background
    background: float = 0.0
    noise: float = 0.0
    level: float = 0.0           # the absolute grey level that was used
    saturated: bool = False


def _cfg(cfg, name, default):
    """Read a tuning value from a Spot-config-like object (or a dict), with a default."""
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(name, default)
    return getattr(cfg, name, default)


def _limit_box(shape, guess_xy, cfg) -> tuple:
    """The search region around ``guess_xy`` as a clipped box (x0, y0, x1, y1);
    the whole frame when there is no guess or no region."""
    h, w = shape[:2]
    half = int(_cfg(cfg, "lookup_region_px", 0) or 0)
    if guess_xy is None or half <= 0:
        return (0, 0, w, h)
    reg = search_region(guess_xy, half, int(_cfg(cfg, "lookup_region_y_px", 0) or 0),
                        _cfg(cfg, "search_shape", "rect"), (w, h))
    return reg["box"] if reg is not None else (0, 0, w, h)


def _ring_mask(shape, box: tuple, width: int):
    """Slices + mask of the ring just outside ``box`` (see _ring_stats)."""
    h, w = shape[:2]
    x0, y0, x1, y1 = box
    X0, Y0 = max(0, x0 - width), max(0, y0 - width)
    X1, Y1 = min(w, x1 + width), min(h, y1 + width)
    m = np.ones((Y1 - Y0, X1 - X0), bool)
    m[y0 - Y0:y1 - Y0, x0 - X0:x1 - X0] = False
    if m.sum() >= 16:
        return (slice(Y0, Y1), slice(X0, X1)), m
    # no room outside (the box fills the image): use the box's own border
    k = max(1, min(width, min(y1 - y0, x1 - x0) // 4))
    m = np.zeros((y1 - y0, x1 - x0), bool)
    m[:k, :] = m[-k:, :] = m[:, :k] = m[:, -k:] = True
    return (slice(y0, y1), slice(x0, x1)), m


def _robust_level(vals: np.ndarray) -> tuple[float, float]:
    """(level, noise) of background pixel values, see _ring_stats."""
    vals = vals.astype(np.float64, copy=False)
    level = float(np.median(vals))
    # an 8-bit camera quantises: even a perfectly quiet background has ~0.3
    # counts of rounding noise (1/sqrt(12)); never claim less than that
    noise = max(0.3, 1.4826 * float(np.median(np.abs(vals - level))))
    for _ in range(3):
        keep = vals[np.abs(vals - level) <= 3.0 * noise + 0.5]
        if keep.size < 8:
            break
        level = float(keep.mean())
        noise = max(0.3, float(keep.std()))
    return level, noise


def _ring_stats(img: np.ndarray, box: tuple, width: int) -> tuple[float, float]:
    """Background level and noise from the ring JUST OUTSIDE ``box``.

    Robust statistics: start from the median and 1.4826 x MAD (a bright
    neighbour or hot pixels in the ring barely move them), then refine to the
    mean and standard deviation of the pixels within 3 of those sigmas. Why
    the refinement: on an 8-bit camera the median and MAD are WHOLE counts --
    a background of 7.6 reads as 7 or 8 frame to frame -- and a 0.5-count
    error in the level, summed over thousands of pixels at large r^2, moves
    sigma^2 far more than the noise does. The clipped mean is continuous.
    When the box already fills the frame there is no "outside", so the box's
    own outermost ``width`` pixels are used instead.
    """
    sl, m = _ring_mask(img.shape, box, width)
    return _robust_level(img[sl][m])


def _saturated(crop: np.ndarray, bright: bool) -> bool:
    if np.issubdtype(crop.dtype, np.integer):
        top = np.iinfo(crop.dtype).max
        return bool((crop >= top).any() if bright else (crop <= 0).any())
    return False


def _signal(crop: np.ndarray, bright: bool) -> np.ndarray:
    """The crop as float, bright = signal (a dark spot is inverted)."""
    a = crop.astype(np.float64)
    if not bright:
        top = float(np.iinfo(crop.dtype).max) if np.issubdtype(crop.dtype, np.integer) else a.max()
        a = top - a
    return a


def _work_area(gray: np.ndarray, lim: tuple, bright: bool, smooth: float, detect: float = 0.0):
    """The search region plus a margin for the background ring and the blurs,
    as float signal (bright = signal), value-smoothed when asked.
    Returns (array, (x0, y0) of the array in the frame)."""
    h, w = gray.shape[:2]
    x0, y0, x1, y1 = lim
    pad = max(x1 - x0, y1 - y0) // 4 + 8 + int(math.ceil(3.0 * (smooth + detect)))
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    # float32: the blurs run twice as fast, and 7 digits are plenty for
    # 8..16-bit pixels (the moment sums themselves are done in float64)
    a = _signal(gray[Y0:Y1, X0:X1], bright).astype(np.float32)
    if smooth > 0.0:
        a = cv2.GaussianBlur(a, (0, 0), smooth, borderType=cv2.BORDER_REFLECT)
    return a, (X0, Y0)


def _mirror_maps(lim: tuple, guess_xy):
    """Index maps from every pixel of the region ``lim`` to its mirror image
    through ``guess_xy`` (the calibrated spot centre), and where that mirror
    lies inside the region at all."""
    X0, Y0, X1, Y1 = lim
    mx2, my2 = int(round(2.0 * guess_xy[0])), int(round(2.0 * guess_xy[1]))
    mxi = mx2 - np.arange(X0, X1) - X0
    myi = my2 - np.arange(Y0, Y1) - Y0
    mx_ok = (mxi >= 0) & (mxi < X1 - X0)
    my_ok = (myi >= 0) & (myi < Y1 - Y0)
    return (np.clip(mxi, 0, X1 - X0 - 1), np.clip(myi, 0, Y1 - Y0 - 1),
            mx_ok, my_ok)


def _filter_clumps(keep: np.ndarray, strength: np.ndarray, thr: float,
                   min_blob: int = 0, mirror=None) -> np.ndarray:
    """Judge every connected clump of ``keep`` as a whole.

    * ``min_blob``: a clump of fewer pixels is a noise peak that made it over
      the bar -- dropped.
    * ``mirror`` (from _mirror_maps): a clump whose MIRROR through the
      calibrated spot centre is mostly background is not part of the spot.
      The spot is centrosymmetric -- its core, its rings and its wings all
      have a twin on the opposite side (round, elliptical or ringed alike) --
      while a neighbouring feature of the sample has none. Weighted by
      ``strength``, a clump needs >= 25 % of its light mirrored (the mirror
      pixel above ``thr``) to stay: the spot's own clumps score near 100 %
      (less for noisy faint wing pieces), an intruder near 0. Judged per
      CLUMP, not per pixel: a per-pixel mirror test also throws away every
      faint wing pixel whose twin happens to be lost in the noise, and that
      biased sigma^2 low in the simulator.
    """
    if not keep.any() or (min_blob <= 1 and mirror is None):
        return keep
    n_lab, lab, stats, _c = cv2.connectedComponentsWithStats(keep.astype(np.uint8), 8)
    good = np.ones(n_lab, bool)
    good[0] = False                               # label 0 = not kept
    if min_blob > 1:
        good[1:] &= stats[1:, cv2.CC_STAT_AREA] >= min_blob
    if mirror is not None:
        mxi, myi, mx_ok, my_ok = mirror
        mir = (strength[myi][:, mxi] >= thr) & my_ok[:, None] & mx_ok[None, :]
        wgt = np.clip(strength, 0.0, None).ravel().astype(np.float64)
        tot = np.bincount(lab.ravel(), wgt, n_lab)
        sym = np.bincount(lab.ravel(), wgt * mir.ravel(), n_lab)
        good[1:] &= sym[1:] >= 0.25 * np.maximum(tot[1:], 1e-12)
    return good[lab]


def spot_second_moment(frame: np.ndarray, guess_xy, cfg=None) -> SpotMoments:
    """Second-moment (D4sigma) size of the spot near ``guess_xy`` (ISO 11146 style).

    ``cfg`` is the Spot config (or anything with the same attribute names, or
    a dict); missing values take the defaults in brackets:
    ``lookup_region_px`` / ``lookup_region_y_px`` / ``search_shape`` bound the
    box (the search region around the guess; the whole frame without one),
    ``clip_sigma`` (3), ``box_factor`` (1.5), ``max_iter`` (10),
    ``clip_mode`` ("local"), ``detect_px`` (2), ``min_blob_px`` (20),
    ``mask_grow_px`` (4), ``smooth_px`` (0), ``reject_asymmetric`` (True),
    ``bright_spot`` (True).

    THE BOX. It starts as the whole search region; each iteration measures the
    centroid and width inside it and re-centres the box on the centroid with a
    half-size of box_factor x D4sigma per axis -- 1.5 makes the box 3 x
    D4sigma wide, the ISO 11146-3 integration area -- until the width changes
    by less than 1 %. If the spot is bigger than the search region the box
    stops at the region (``clipped`` = True): make the region larger than ~3 x
    the biggest D4sigma you want measured.

    WHICH PIXELS COUNT, after subtracting the background (clip_mode):
      * "pixel" -- the textbook clip: a pixel counts when its own value is
        above clip_sigma x the noise. BIASED for a dim, spread-out spot: its
        wings are a count or two above the background, below 3 x the noise of
        ONE pixel, so they are deleted -- and the wings carry most of r^2. In
        the coherent simulator (noise 1-3 counts) this made sigma^2 30-65 %
        too small out of focus and moved the parabola's vertex by ~0.7
        Rayleigh ranges (tests/test_spot_moments.py). Kept to compare.
      * "local" (default) -- decide on a SMOOTHED copy, measure on the raw
        image: a pixel counts when the image blurred by a Gaussian of
        ``detect_px`` is above clip_sigma x the noise OF THAT BLURRED IMAGE
        (measured in the same background ring, so correlated camera noise is
        handled too). Blurring by 2 px lowers the noise ~7x, so wings a
        fraction of a count above the background are found. Then clumps
        smaller than ``min_blob_px`` are dropped (noise peaks that made it
        over the bar) and the mask is widened by ``mask_grow_px`` so the
        faint fringe of every wing is inside it, and the moments use the RAW
        background-subtracted values in the mask, negative noise included.
        The reasoning: a background pixel wrongly kept adds noise with mean
        zero -- scatter, no bias; a spot pixel wrongly dropped always makes
        sigma^2 smaller -- bias. So the mask is generous on purpose.
    ``smooth_px`` > 0 blurs the image itself first (the values, not only the
    decision): the blur adds EXACTLY smooth_px^2 to sigma_x^2 and sigma_y^2
    (the variances of a convolution add), and that constant is subtracted
    again, so it cannot move the vertex. It barely reduces the scatter either
    (the moments are sums, the blur only moves noise around), which is why
    detect_px does the noise work and smooth_px defaults to 0.
    ``reject_asymmetric`` drops every clump of kept pixels that has no twin
    on the opposite side of the guess (the calibrated spot centre): another
    bright object reaching into the search region (see _filter_clumps). In
    the simulator the sample's pattern 120 px from the spot made sigma^2 up
    to 30 x too big without it.
    """
    gray = to_gray(frame) if frame.ndim == 3 else frame
    bright = bool(_cfg(cfg, "bright_spot", True))
    clip_k = max(0.0, float(_cfg(cfg, "clip_sigma", 3.0)))
    factor = max(0.5, float(_cfg(cfg, "box_factor", 1.5)))
    max_iter = max(1, int(_cfg(cfg, "max_iter", 10)))
    smooth = max(0.0, float(_cfg(cfg, "smooth_px", 0.0)))
    textbook = str(_cfg(cfg, "clip_mode", "local")) == "pixel"
    detect = max(0.0, float(_cfg(cfg, "detect_px", 2.0)))
    min_blob = max(0, int(_cfg(cfg, "min_blob_px", 20)))
    grow = max(0, int(_cfg(cfg, "mask_grow_px", 4)))
    symmetric = bool(_cfg(cfg, "reject_asymmetric", True)) and guess_xy is not None
    lim = _limit_box(gray.shape, guess_xy, cfg)
    LX0, LY0, LX1, LY1 = lim
    work, (WX0, WY0) = _work_area(gray, lim, bright, smooth, detect)
    rx0, ry0, rx1, ry1 = LX0 - WX0, LY0 - WY0, LX1 - WX0, LY1 - WY0   # region in `work`
    if textbook:
        det_work = work
    elif detect > 0.0:
        det_work = cv2.GaussianBlur(work, (0, 0), detect, borderType=cv2.BORDER_REFLECT)
    else:
        det_work = cv2.blur(work, (3, 3), borderType=cv2.BORDER_REFLECT)
    mirror = _mirror_maps(lim, guess_xy) if symmetric else None
    box = lim
    out = SpotMoments(box=box)
    prev_d4 = None
    for it in range(1, max_iter + 1):
        x0, y0, x1, y1 = box
        # background + noise from the ring just outside the box (a quarter of
        # the box wide, 4..12 px: thousands of pixels, enough for robust
        # statistics); the decision image's own noise from the same pixels
        ring = min(12, max(4, min(x1 - x0, y1 - y0) // 4))
        wbox = (x0 - WX0, y0 - WY0, x1 - WX0, y1 - WY0)
        rsl, rm = _ring_mask(work.shape, wbox, ring)
        bg, noise = _robust_level(work[rsl][rm])
        dnoise = noise if textbook else _robust_level(det_work[rsl][rm])[1]
        reg = work[ry0:ry1, rx0:rx1] - bg
        det = det_work[ry0:ry1, rx0:rx1] - bg
        thr = clip_k * dnoise
        keep = det >= thr
        keep = _filter_clumps(keep, det, thr, 0 if textbook else min_blob, mirror)
        if not textbook and grow > 0:
            k = np.ones((2 * grow + 1, 2 * grow + 1), np.uint8)
            keep = cv2.dilate(keep.astype(np.uint8), k) > 0
        sl = (slice(y0 - LY0, y1 - LY0), slice(x0 - LX0, x1 - LX0))
        sig = np.where(keep[sl], reg[sl], 0.0).astype(np.float64)
        peak = float(reg[sl].max())
        m0 = float(sig.sum())
        out.n_iter, out.background, out.noise, out.peak = it, bg, noise, peak
        out.saturated = _saturated(gray[y0:y1, x0:x1], bright)
        out.box = box
        if m0 <= 0.0 or not keep[sl].any() or peak <= clip_k * noise:
            out.ok, out.why = False, "no spot above the noise"
            return out
        # moments from the row/column sums: two 1-D dot products instead of a
        # full 2-D r^2 map (cheap: the box is at most the search region)
        xs = np.arange(x0, x1, dtype=np.float64)
        ys = np.arange(y0, y1, dtype=np.float64)
        px, py = sig.sum(axis=0), sig.sum(axis=1)
        cx, cy = float(px @ xs) / m0, float(py @ ys) / m0
        dx, dy = xs - cx, ys - cy
        # minus the value blur's own variance: what is left is the spot's
        sxx = float(px @ (dx * dx)) / m0 - smooth * smooth
        syy = float(py @ (dy * dy)) / m0 - smooth * smooth
        sxy = float(dy @ (sig @ dx)) / m0
        if sxx <= 0.0 or syy <= 0.0:
            out.ok, out.why = False, "no measurable width"
            return out
        d4x, d4y = 4.0 * math.sqrt(sxx), 4.0 * math.sqrt(syy)
        d4 = 4.0 * math.sqrt(0.5 * (sxx + syy))
        out.ok, out.why = True, ""
        out.cx, out.cy, out.total = cx, cy, m0
        out.sigma2_x, out.sigma2_y, out.sigma2_xy = sxx, syy, sxy
        out.sigma2 = 0.5 * (sxx + syy)
        out.d4sigma, out.d4sigma_x, out.d4sigma_y = d4, d4x, d4y
        if prev_d4 is not None and abs(d4 - prev_d4) <= 0.01 * prev_d4:
            out.converged = True
            break
        prev_d4 = d4
        # the next box: centred on the centroid, box_factor x D4sigma each way
        # (min 3 px, plus the blur's width), never beyond the search region
        hx = max(3.0, factor * math.sqrt(d4x ** 2 + 16.0 * smooth ** 2))
        hy = max(3.0, factor * math.sqrt(d4y ** 2 + 16.0 * smooth ** 2))
        want = (int(math.floor(cx - hx)), int(math.floor(cy - hy)),
                int(math.ceil(cx + hx)) + 1, int(math.ceil(cy + hy)) + 1)
        nb = (max(want[0], LX0), max(want[1], LY0), min(want[2], LX1), min(want[3], LY1))
        out.clipped = nb != want
        if nb[2] <= nb[0] or nb[3] <= nb[1]:
            out.ok, out.why = False, "the centroid left the search region"
            return out
        if nb == box:                        # nothing left to change
            out.converged = True
            break
        box = nb
    return out


def spot_relative_area(frame: np.ndarray, guess_xy, cfg=None) -> SpotRelArea:
    """Area of the pixels brighter than ``rel_level`` x the spot's own peak.

    ``rel_level`` (cfg, default 0.135 = 1/e^2) is a fraction of the peak ABOVE
    the background, so the threshold follows the spot as defocus dims it --
    unlike the fixed threshold. Searched in the same region as the per-frame
    spot check (around ``guess_xy``). Every selected pixel counts, rings
    included; the centroid is their plain mean.

    The level never drops below clip_sigma x the background noise: far out of
    focus 13.5 % of a dim peak is inside the noise, and the "area" would then
    be a count of noisy background pixels. With ``reject_asymmetric`` (default)
    a bright neighbour without a twin opposite the calibrated centre is left
    out of both the peak and the area (see _filter_clumps).

    Expected to break where the coherent spot has a hole: the peak then sits on
    the ring, and the area jumps as the ring and the centre trade places. That
    is why the second moment is the default candidate; this one is here to be
    compared on the rig (Lukas, 2026-09-28).
    """
    gray = to_gray(frame) if frame.ndim == 3 else frame
    bright = bool(_cfg(cfg, "bright_spot", True))
    rel = min(max(float(_cfg(cfg, "rel_level", 0.135)), 0.001), 0.999)
    clip_k = max(0.0, float(_cfg(cfg, "clip_sigma", 3.0)))
    smooth = max(0.0, float(_cfg(cfg, "smooth_px", 0.0)))
    x0, y0, x1, y1 = box = _limit_box(gray.shape, guess_xy, cfg)
    crop = gray[y0:y1, x0:x1]
    out = SpotRelArea()
    if crop.size == 0:
        out.why = "empty search region"
        return out
    ring = min(12, max(4, min(x1 - x0, y1 - y0) // 8))
    bg, noise = _ring_stats(gray if bright else _signal(gray, False), box, ring)
    sig = _signal(crop, bright)
    if smooth > 0.0:
        sig = cv2.GaussianBlur(sig, (0, 0), smooth)
    out.background, out.noise = bg, noise
    out.saturated = _saturated(crop, bright)
    # which clumps are the spot at all: significantly above the background and,
    # with reject_asymmetric, with a twin opposite the calibrated centre -- so
    # a brighter neighbour can neither set the peak nor add to the area
    blur3 = cv2.blur(sig, (3, 3)) - bg
    spot = blur3 >= clip_k * noise / 3.0
    symmetric = bool(_cfg(cfg, "reject_asymmetric", True)) and guess_xy is not None
    spot = _filter_clumps(spot, blur3, clip_k * noise / 3.0, 0,
                          _mirror_maps(box, guess_xy) if symmetric else None)
    # the peak from a 3x3 average: one noisy or hot pixel must not set the level
    peak = float(blur3[spot].max()) if spot.any() else 0.0
    out.peak = peak
    if peak <= clip_k * noise:
        out.why = "no spot above the noise"
        return out
    level = bg + max(rel * peak, clip_k * noise)
    mask = (sig >= level) & spot
    if (_cfg(cfg, "search_shape", "rect") == "circle" and guess_xy is not None
            and int(_cfg(cfg, "lookup_region_px", 0) or 0) > 0):
        r = int(_cfg(cfg, "lookup_region_px", 0))
        yy, xx = np.ogrid[y0:y1, x0:x1]
        mask &= (xx - guess_xy[0]) ** 2 + (yy - guess_xy[1]) ** 2 <= r * r
    n = int(np.count_nonzero(mask))
    out.level = level
    if n == 0:
        out.why = "nothing above the level"
        return out
    ys, xs = np.nonzero(mask)
    out.ok = True
    out.area = float(n)
    out.cx, out.cy = float(xs.mean() + x0), float(ys.mean() + y0)
    return out


# --------------------------------------------------------------------------- #
# Template / pattern matching
# --------------------------------------------------------------------------- #
@dataclass
class MatchReport:
    found: bool = False
    x: float = 0.0           # matched template CENTRE, pixels
    y: float = 0.0
    score: float = 0.0       # 0..1 normalised correlation
    angle: float = 0.0       # best rotation, degrees


def _rotate_template(tpl: np.ndarray, angle: float) -> np.ndarray:
    h, w = tpl.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    return cv2.warpAffine(tpl, m, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def match_template(
    image: np.ndarray,
    template: np.ndarray,
    min_score: float = 0.6,
    angle_start: float = 0.0,
    angle_end: float = 0.0,
    angle_step: float = 2.0,
    search_box: tuple[int, int, int, int] | None = None,
) -> MatchReport:
    """Find ``template`` in ``image`` by normalised cross-correlation.

    Ports the NI 'Grayscale Value Pyramid' matcher's job with OpenCV's
    ``matchTemplate`` (TM_CCOEFF_NORMED).  If an angle range is given, the
    template is matched at several rotations and the best kept.  ``search_box``
    (x, y, w, h) restricts the search (the 'safety area').  The returned (x, y)
    is the template CENTRE in full-frame pixels.
    """
    gray = to_gray(image)
    tpl = to_gray(template)
    ox, oy = 0, 0
    if search_box is not None:
        H, W = gray.shape
        x, y, w, h = search_box
        x = max(0, min(int(x), W - 1)); y = max(0, min(int(y), H - 1))
        w = min(int(w), W - x); h = min(int(h), H - y)
        if w > tpl.shape[1] and h > tpl.shape[0]:
            gray = gray[y:y + h, x:x + w]
            ox, oy = x, y

    if gray.shape[0] < tpl.shape[0] or gray.shape[1] < tpl.shape[1]:
        return MatchReport(found=False)

    # Angle list: single 0 when no range requested.
    if angle_end > angle_start and angle_step > 0:
        angles = list(np.arange(angle_start, angle_end + 1e-9, angle_step))
    elif angle_start != 0.0:
        angles = [angle_start]
    else:
        angles = [0.0]

    best = MatchReport(found=False)
    th, tw = tpl.shape[:2]
    for ang in angles:
        t = _rotate_template(tpl, ang) if ang else tpl
        res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > best.score:
            sx, sy = _subpixel_peak(res, max_loc)
            best = MatchReport(
                found=bool(max_val >= min_score),
                x=max_loc[0] + sx + tw / 2.0 + ox,
                y=max_loc[1] + sy + th / 2.0 + oy,
                score=float(max_val),
                angle=float(ang),
            )
    return best


def _subpixel_peak(res: np.ndarray, loc) -> tuple[float, float]:
    """Refine a correlation peak to sub-pixel by 1-D parabolic interpolation."""
    x, y = loc
    h, w = res.shape
    dx = dy = 0.0
    if 0 < x < w - 1:
        l, c, r = res[y, x - 1], res[y, x], res[y, x + 1]
        denom = (l - 2 * c + r)
        if abs(denom) > 1e-9:
            dx = 0.5 * (l - r) / denom
    if 0 < y < h - 1:
        u, c, d = res[y - 1, x], res[y, x], res[y + 1, x]
        denom = (u - 2 * c + d)
        if abs(denom) > 1e-9:
            dy = 0.5 * (u - d) / denom
    # A valid parabolic offset is within +/-1 px; clamp against noise.
    return (float(np.clip(dx, -1, 1)), float(np.clip(dy, -1, 1)))


# --------------------------------------------------------------------------- #
# Autofocus scoring
# --------------------------------------------------------------------------- #
def focus_metric(
    image: np.ndarray,
    mechanism: str = "spot_area",
    roi: tuple[int, int, int, int] | None = None,
    spot_thr: tuple[int, int] = (200, 255),
) -> float:
    """Score how well focused ``image`` is by the chosen mechanism.

    * ``spot_area`` -- thresholded spot area (px).  SMALLER = better focus,
      so callers minimise (see :data:`FOCUS_MAXIMISE`).
    * ``edges``     -- variance of the Laplacian (edge sharpness).  Larger=better.
    * ``fft``       -- fraction of spectral energy at high spatial frequency.
      Larger = better.
    """
    gray = to_gray(image)
    if roi is not None:
        x, y, w, h = roi
        H, W = gray.shape
        x = max(0, min(int(x), W - 1)); y = max(0, min(int(y), H - 1))
        w = min(int(w), W - x); h = min(int(h), H - y)
        if w > 1 and h > 1:
            gray = gray[y:y + h, x:x + w]

    if mechanism == "edges":
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    if mechanism == "fft":
        f = np.fft.fftshift(np.fft.fft2(gray.astype(np.float64)))
        mag = np.abs(f)
        h, w = mag.shape
        cy, cx = h / 2.0, w / 2.0
        yy, xx = np.ogrid[:h, :w]
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        r0 = 0.15 * min(h, w)          # "high frequency" starts here
        total = mag.sum() + 1e-9
        return float(mag[r > r0].sum() / total)

    if mechanism in ("spot_d4sigma", "spot_relative"):
        # no calibrated position here (the brain passes one): start from the
        # brightest point of a lightly blurred copy, search the whole image
        y, x = np.unravel_index(int(np.argmax(cv2.blur(gray, (5, 5)))), gray.shape)
        if mechanism == "spot_d4sigma":
            r = spot_second_moment(gray, (float(x), float(y)), None)
            return float(r.sigma2) if r.ok else float("nan")
        r = spot_relative_area(gray, (float(x), float(y)), None)
        return float(r.area) if r.ok else float("nan")

    # default: spot_area
    mask = cv2.inRange(gray, int(spot_thr[0]), int(spot_thr[1]))
    return float(cv2.countNonZero(mask))


def focus_is_maximised(mechanism: str) -> bool:
    return FOCUS_MAXIMISE.get(mechanism, True)


def best_focus_from_sweep(
    z_levels, metrics, maximise: bool = True, fit_curve: bool = True,
    rel_window: float | None = None,
) -> float:
    """Given a Z sweep, return the Z of best focus.

    ``fit_curve`` fits a parabola near the extremum and returns its vertex
    (sub-step precision); otherwise the raw argmax/argmin Z is returned.
    The fit uses +-2 levels around the extremum -- or, with ``rel_window``
    (minimising only), every level whose metric is at most rel_window x the
    minimum: for the second moment sigma^2, which IS a parabola in Z, more of
    the curve gives a better vertex than five points at the bottom.
    """
    z = np.asarray(z_levels, dtype=float)
    m = np.asarray(metrics, dtype=float)
    if z.size == 0:
        return 0.0
    idx = int(np.argmax(m) if maximise else np.argmin(m))
    if not fit_curve or z.size < 3:
        return float(z[idx])

    # Fit a parabola to a small window around the extremum for sub-step accuracy.
    lo = max(0, idx - 2)
    hi = min(z.size, idx + 3)
    zz, mm = z[lo:hi], m[lo:hi]
    if rel_window and not maximise and m[idx] > 0:
        # the contiguous run of levels around the minimum within the window
        # (contiguous: a far level that dips back in is not part of this dip)
        lo2 = idx
        while lo2 > 0 and m[lo2 - 1] <= rel_window * m[idx]:
            lo2 -= 1
        hi2 = idx + 1
        while hi2 < z.size and m[hi2] <= rel_window * m[idx]:
            hi2 += 1
        if hi2 - lo2 > zz.size:
            zz, mm = z[lo2:hi2], m[lo2:hi2]
    if zz.size < 3:
        return float(z[idx])
    try:
        a, b, _c = np.polyfit(zz, mm, 2)
        if abs(a) < 1e-12:
            return float(z[idx])
        vertex = -b / (2 * a)
        # Reject a nonsense vertex (wrong curvature or far outside the window).
        concave_down = a < 0
        if concave_down != maximise:
            return float(z[idx])
        if vertex < zz.min() - abs(zz[1] - zz[0]) or vertex > zz.max() + abs(zz[1] - zz[0]):
            return float(z[idx])
        return float(vertex)
    except Exception:
        return float(z[idx])


# --------------------------------------------------------------------------- #
# Scanning-array geometry (pixel offsets from the array centre)
# --------------------------------------------------------------------------- #
def scanning_array_pixel_offsets(
    points_x: int,
    points_y: int,
    dx_um: float,
    dy_um: float,
    angle_deg: float,
    pixel_size_x_um: float,
    pixel_size_y_um: float,
) -> np.ndarray:
    """Return the centred pixel offsets of every scanning point.

    Ports ScanningArrayPixelDistancesCentered.vi: a ``points_x`` x ``points_y``
    grid with pitch (dx, dy) in um, rotated by ``angle_deg``, converted to pixels
    and centred so the middle of the array is (0, 0).  Output shape
    ``(points_y, points_x, 2)`` with [..., 0]=dx_px, [..., 1]=dy_px, ordered
    row-major (iy outer, ix inner) -- matching the LabVIEW 'Array XY - pure'.

    Pixels use image convention (y DOWN); a positive stage-um step in +y maps to
    +y pixels via ``pixel_size_y_um``.
    """
    ca, sa = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    out = np.zeros((points_y, points_x, 2), dtype=float)
    cx = (points_x - 1) / 2.0
    cy = (points_y - 1) / 2.0
    for iy in range(points_y):
        for ix in range(points_x):
            ux = (ix - cx) * dx_um          # um offset from centre
            uy = (iy - cy) * dy_um
            rx = ux * ca - uy * sa          # rotate in the sample plane
            ry = ux * sa + uy * ca
            out[iy, ix, 0] = rx / pixel_size_x_um    # -> pixels
            out[iy, ix, 1] = ry / pixel_size_y_um
    return out


@dataclass
class ScanGeometry:
    """Result of pinning the scanning array to the template for one frame."""

    array_center_px: tuple = (0.0, 0.0)     # where the array centre sits (px)
    selected_point_px: tuple = (0.0, 0.0)   # the targeted point (px)
    point_minus_spot_px: tuple = (0.0, 0.0) # selected_point - spot (px) to null
    spot_at_index: tuple = (0, 0)           # nearest array index to the spot
    offsets: np.ndarray = field(default=None)  # (ny, nx, 2) centred pixel offsets


def pin_array_and_distance(
    template_xy: tuple[float, float],
    array_center_offset_px: tuple[float, float],
    offsets: np.ndarray,
    selected_ix: int,
    selected_iy: int,
    spot_xy: tuple[float, float],
) -> ScanGeometry:
    """Pin the array to the template and compute the stabiliser distance.

    ``array_center_offset_px`` is the (constant) vector from the template match
    point to the centre of the scanning array -- i.e. where you placed the array
    when you defined it (the 'Template-array distance' in the LabVIEW hidden tab).
    The array centre in the current frame is therefore ``template_xy +
    array_center_offset``.
    """
    ny, nx = offsets.shape[:2]
    six = int(np.clip(selected_ix, 0, nx - 1))
    siy = int(np.clip(selected_iy, 0, ny - 1))

    acx = template_xy[0] + array_center_offset_px[0]
    acy = template_xy[1] + array_center_offset_px[1]

    sel = offsets[siy, six]
    selp = (acx + sel[0], acy + sel[1])
    dmv = (selp[0] - spot_xy[0], selp[1] - spot_xy[1])

    # Which grid point is the spot currently nearest to?
    pts = offsets.reshape(-1, 2) + np.array([acx, acy])
    d2 = ((pts[:, 0] - spot_xy[0]) ** 2 + (pts[:, 1] - spot_xy[1]) ** 2)
    k = int(np.argmin(d2))
    spot_ix, spot_iy = k % nx, k // nx

    return ScanGeometry(
        array_center_px=(acx, acy),
        selected_point_px=selp,
        point_minus_spot_px=dmv,
        spot_at_index=(spot_ix, spot_iy),
        offsets=offsets,
    )


def pixels_to_um(dx_px: float, dy_px: float, px_x: float, px_y: float) -> tuple:
    """Convert a pixel displacement to a stage displacement in um."""
    return (dx_px * px_x, dy_px * px_y)
