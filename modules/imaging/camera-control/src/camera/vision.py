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
                  "spot_d4sigma": False, "spot_relative": False,
                  # 2026-09-29: encircled-energy radius^2 and the Gaussian
                  # fit's sigma^2 are sizes (minimise); the spot's PEAK
                  # brightness is highest at focus (maximise)
                  "spot_encircled": False, "spot_gauss": False, "spot_peak": True}


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
    # Not found: WHY, in words (2026-09-29, Lukas's screenshot: "spot not seen"
    # while the threshold-free sizes measured the spot fine -- the rig's
    # max_area_px 2000 rejected the 2600-4400 px^2 spot out of focus, and
    # nothing said so). ``why`` = a sentence with the numbers and what to
    # change; ``why_short`` = a few words for a label on the image.
    why: str = ""
    why_short: str = ""


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


# --------------------------------------------------------------------------- #
# The LARGEST a spot may be: a fraction of the search region (2026-09-29 late)
# --------------------------------------------------------------------------- #
# max_area_px was introduced on 2026-09-14 to keep a saturated illuminated
# corner (396 000 px) from being taken for the laser. Since 9da7a07 every spot
# search is confined to the search region and the candidate rules (reaching
# out of the region, elongated, centred outside it) reject such a patch by its
# SHAPE -- and the fixed 2000 px^2 on the rig then rejected the LASER itself:
# at the working exposure (2480 us) it is a saturated disc with rings of
# 3.4-7.8 k px^2 above the locate threshold, and locate returned a 30-50 px
# ring fragment 45-52 px away instead. So max_area_px = 0 (now the default)
# means "a quarter of the search region's area": a spot filling more than
# that is not a spot IN the region any more, whatever the exposure. An
# explicit value still works (the why-text says when it rejected the spot).
AUTO_MAX_AREA_FRACTION = 0.25


def region_area(reg) -> float:
    """Area (px^2) of a search region from :func:`search_region` (unclipped)."""
    hx, hy = reg["half"]
    if reg["shape"] == "circle":
        return math.pi * hx * hx
    return float((2 * hx + 1) * (2 * hy + 1))


def max_area_limit(cfg, reg) -> float:
    """The largest blob (px^2) that may be the spot: Spot.max_area_px when it is
    set (> 0), else AUTO_MAX_AREA_FRACTION of the search region; 0 = no limit
    (0 and no region: the whole-frame opt-out)."""
    explicit = int(_cfg(cfg, "max_area_px", 0) or 0)
    if explicit > 0:
        return float(explicit)
    return AUTO_MAX_AREA_FRACTION * region_area(reg) if reg is not None else 0.0


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


def _why_no_candidate(stats, labels, ok, offset, full_size, centre_xy, min_area_px,
                      max_area_px, reject_border, max_area_is_auto=False):
    """(why, why_short) when no thresholded blob was a candidate.

    The blob AT the calibrated position (``centre_xy``, full-frame px) is the
    one the user means by "the spot": when it exists and was rejected, its own
    reason is given with its numbers. Otherwise the rejected blobs are
    counted by reason.
    """
    s = stats[1:]
    area = s[:, cv2.CC_STAT_AREA]
    x0 = s[:, cv2.CC_STAT_LEFT] + offset[0]
    y0 = s[:, cv2.CC_STAT_TOP] + offset[1]
    x1 = x0 + s[:, cv2.CC_STAT_WIDTH]
    y1 = y0 + s[:, cv2.CC_STAT_HEIGHT]
    small = area < max(1, int(min_area_px))
    large = (area > int(max_area_px)) if (max_area_px and max_area_px > 0) else \
        np.zeros_like(small)
    edge = (((x0 <= 0) | (y0 <= 0) | (x1 >= full_size[0]) | (y1 >= full_size[1]))
            if reject_border else np.zeros_like(small))
    if max_area_is_auto:
        big_hint = ("the automatic limit is a quarter of the search region -- enlarge the "
                    "search region (Spot tab)")
    else:
        big_hint = "set max area to 0 (automatic) or raise it (Spot tab)"
    j = None
    if centre_xy is not None:
        # the label under the calibrated position; a donut (dark centre) has
        # none there, so fall back to the largest blob whose box holds it
        cx, cy = int(round(centre_xy[0])) - offset[0], int(round(centre_xy[1])) - offset[1]
        if 0 <= cy < labels.shape[0] and 0 <= cx < labels.shape[1] and labels[cy, cx] > 0:
            j = int(labels[cy, cx]) - 1
        else:
            gx, gy = centre_xy
            holds = (x0 <= gx) & (gx < x1) & (y0 <= gy) & (gy < y1)
            if holds.any():
                j = int(np.argmax(np.where(holds, area, -1)))
    if j is not None and not ok[j]:
        a = int(area[j])
        if large[j]:
            return (f"the blob at the calibrated position is {a} px, larger than max area "
                    f"{int(max_area_px)} px -- {big_hint}", "larger than max area")
        if small[j]:
            return (f"the blob at the calibrated position is {a} px, smaller than min area "
                    f"{int(min_area_px)} px (Spot tab)", "smaller than min area")
        if edge[j]:
            return ("the blob at the calibrated position touches the frame edge "
                    "(Spot tab: reject border)", "touches the frame edge")
    parts = []
    if large.any():
        parts.append(f"{int(large.sum())} larger than max area {int(max_area_px)} px "
                     f"(largest {int(area[large].max())} px)")
    if small.any():
        parts.append(f"{int(small.sum())} smaller than min area {int(min_area_px)} px")
    if edge.any():
        parts.append(f"{int(edge.sum())} touching the frame edge")
    why = "no blob above the threshold was a candidate: " + ", ".join(parts)
    if large.any() and not small.any() and not edge.any():
        return why + f" -- {big_hint}", "larger than max area"
    if small.any() and not large.any() and not edge.any():
        return why, "smaller than min area"
    return why, "no candidate blob"


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
    max_area_is_auto: bool = False,
) -> SpotReport:
    """Locate the laser spot by intensity threshold + centre of mass.

    If ``lookup_region_px`` > 0 and ``last_xy`` is given, only a region around
    that point is searched (speed / robustness), see :func:`search_region`:
    ``search_shape`` "rect" = +/- ``lookup_region_px`` in x and
    +/- ``lookup_region_y_px`` in y (0 = same as x), "circle" = radius
    ``lookup_region_px``. Coordinates are still returned in FULL-frame pixels.

    Candidates are the thresholded blobs with ``min_area_px <= area`` and, when
    given, ``area <= max_area_px`` (0 = no limit here; the camera passes
    :func:`max_area_limit`, i.e. Spot.max_area_px 0 -> a quarter of the
    region); with ``reject_border`` a blob touching the
    edge of the FULL frame is not a candidate either. The largest candidate wins.
    Why the two filters (lab rig, 63x, 2026-09-14): saturated illumination
    filling a corner of the frame was a 396 000 px blob touching the border, the
    real laser spot a 1 300 px blob in the middle -- and "largest blob" picked
    the corner, so the calibrated spot position was the centre of that corner.

    Not found: ``why`` / ``why_short`` say why (see SpotReport) --
    ``max_area_is_auto`` = the caller's ``max_area_px`` is the automatic limit
    (Spot.max_area_px 0), which changes what the advice says.
    """
    lo_user = int(thr_lower)
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
        top = int(gray.max()) if gray.size else 0
        if not bright_spot:
            top = 255 - top
        why = (f"nothing above the threshold {lo_user} in the search region "
               f"(brightest there {top})" if bright_spot else
               f"nothing below the threshold in the search region")
        if unsym is not None and unsym.any():
            why = ("light above the threshold, but none of it symmetric about the "
                   "calibrated position (Spot tab: symmetric) -- recalibrate the spot "
                   "or switch symmetric off")
            return SpotReport(found=False, why=why, why_short="not symmetric")
        return SpotReport(found=False, why=why, why_short="nothing above the threshold")

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return SpotReport(found=False, why="nothing above the threshold in the search region",
                          why_short="nothing above the threshold")
    ok = spot_candidates(stats, (ox, oy), (full_w, full_h), min_area_px, max_area_px,
                         reject_border)
    if not ok.any():
        why, short = _why_no_candidate(stats, labels, ok, (ox, oy), (full_w, full_h),
                                       last_xy, min_area_px, max_area_px, reject_border,
                                       max_area_is_auto)
        return SpotReport(found=False, why=why, why_short=short)
    if center_rc is not None:
        # the candidate CENTRED on the calibrated position (a symmetric blob's
        # centroid is the centre of symmetry; mirror pairs of blobs are not)
        d = np.hypot(centroids[1:, 0] - center_rc[0], centroids[1:, 1] - center_rc[1])
        d = np.where(ok, d, np.inf)
        j = int(np.argmin(d))
        if not d[j] <= 1.5:
            # the blob AT the centre may have been rejected (area, edge): say so
            why, short = _why_no_candidate(stats, labels, ok, (ox, oy), (full_w, full_h),
                                           last_xy, min_area_px, max_area_px,
                                           reject_border, max_area_is_auto)
            if not short.startswith(("larger", "smaller", "touches")):
                why = ("no blob above the threshold is centred on the calibrated position "
                       "(Spot tab: symmetric) -- recalibrate the spot or switch symmetric off")
                short = "not centred on the calibration"
            return SpotReport(found=False, why=why, why_short=short)
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
#     small there (rig 2026-09-28: up to 50 % low, peak 11-16 grey levels).
#     So the brain hands these functions the camera's FULL-DEPTH frame when
#     the camera delivers one (Mono10/12: backends grab once, deliver both,
#     never change the camera's PixelFormat) -- see Camera._spot_source.
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
    n_saturated: int = 0         # selected spot pixels AT the camera's full scale


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


def _full_scale(crop: np.ndarray, max_value=None):
    """The camera's maximum count for this frame, or None if unknown.

    ``max_value`` is the TRUE full scale when the caller knows it: a 12-bit
    camera frame arrives in a uint16 container, whose own maximum (65535) is
    never reached -- saturation there is 4095 (2026-09-28, 12-bit frames).
    Without it: the dtype's maximum for integer frames, unknown for float.
    """
    if max_value is not None:
        return float(max_value)
    if np.issubdtype(crop.dtype, np.integer):
        return float(np.iinfo(crop.dtype).max)
    return None


def _saturated(crop: np.ndarray, bright: bool, max_value=None) -> bool:
    top = _full_scale(crop, max_value)
    if top is None:
        return False
    return bool((crop >= top).any() if bright else (crop <= 0).any())


def _signal(crop: np.ndarray, bright: bool, max_value=None) -> np.ndarray:
    """The crop as float, bright = signal (a dark spot is inverted)."""
    a = crop.astype(np.float64)
    if not bright:
        top = _full_scale(crop, max_value)
        a = (a.max() if top is None else top) - a
    return a


def _work_area(gray: np.ndarray, lim: tuple, bright: bool, smooth: float, detect: float = 0.0,
               max_value=None):
    """The search region plus a margin for the background ring and the blurs,
    as float signal (bright = signal), value-smoothed when asked.
    Returns (array, (x0, y0) of the array in the frame)."""
    h, w = gray.shape[:2]
    x0, y0, x1, y1 = lim
    pad = max(x1 - x0, y1 - y0) // 4 + 8 + int(math.ceil(3.0 * (smooth + detect)))
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    # float32: the blurs run twice as fast, and 7 digits are plenty for
    # 8..16-bit pixels (the moment sums themselves are done in float64)
    a = _signal(gray[Y0:Y1, X0:X1], bright, max_value).astype(np.float32)
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


def spot_second_moment(frame: np.ndarray, guess_xy, cfg=None, max_value=None) -> SpotMoments:
    """Second-moment (D4sigma) size of the spot near ``guess_xy`` (ISO 11146 style).

    ``frame`` may be the 8-bit frame or the camera's full-depth one (uint16
    holding 10/12-bit counts, or a float average of them); ``max_value`` is
    then the camera's full scale (4095 for 12 bit), used only to flag
    saturation. Every other number here is relative to the background noise
    or to the spot itself, so the result does not depend on the depth -- except
    that the faint wings are no longer rounded away (see "dynamic range" above).

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
    work, (WX0, WY0) = _work_area(gray, lim, bright, smooth, detect, max_value)
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
        out.saturated = _saturated(gray[y0:y1, x0:x1], bright, max_value)
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


def spot_relative_area(frame: np.ndarray, guess_xy, cfg=None, max_value=None) -> SpotRelArea:
    """Area of the pixels brighter than ``rel_level`` x the spot's own peak.

    Works on the 8-bit or the full-depth frame alike (``max_value`` = the
    camera's full scale, for the saturation flag): the level is a FRACTION of
    the spot's own peak, so it means the same at any bit depth.

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
    bg, noise = _ring_stats(gray if bright else _signal(gray, False, max_value), box, ring)
    sig = _signal(crop, bright, max_value)
    if smooth > 0.0:
        sig = cv2.GaussianBlur(sig, (0, 0), smooth)
    out.background, out.noise = bg, noise
    out.saturated = _saturated(crop, bright, max_value)
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
    top = _full_scale(crop, max_value)
    if top is not None and bright:
        out.n_saturated = int(np.count_nonzero(crop[mask] >= top))
    return out


# --------------------------------------------------------------------------- #
# WHERE the spot is, apart from HOW BIG it is (2026-09-29)
# --------------------------------------------------------------------------- #
# Rig, Lukas's screenshot: the spot plainly visible ~100 px from its calibrated
# position, at the edge of the search region, and every threshold-free size
# said "no spot above the noise". Both measurements were centred on the
# CALIBRATED position, and reject_asymmetric mirrors the light through that
# centre -- a spot that is not exactly there has no twin and is thrown away,
# all of it. So the CENTRE of the size measurement is now a choice (Spot.locate):
#   "calibrated" -- as before (the default: the position is a user calibration,
#                   done once in a while, never tracked frame by frame);
#   "peak"       -- the brightest point of the chosen spot candidate (below),
#                   sub-pixel; the middle of its flat top when it is saturated;
#   "blob"       -- the chosen candidate's intensity centroid.
# The measurement box and the mirror test then use the LOCATED centre. The
# calibrated position is still THE position for motion (stabiliser, click to go).
#
# THE LASER AMONG OTHER BRIGHT LIGHT (rig check of 6da4e31, 2026-09-29).
# Lukas's rule, the same day: "Always look for the laser spot in the safety
# area around the laser only!" -- every search here (locate, the why-text, the
# calibration) looks ONLY in the search region (lookup_region_px /
# lookup_region_y_px / search_shape) around the calibrated laser position; with
# no calibration yet, around the frame centre. lookup_region_px = 0 is the one
# explicit exception (the old meaning: no region, the whole frame).
# Inside that region a CANDIDATE must look like a laser spot:
#   * above the threshold max(locate_k x noise, locate_rel x the region's
#     brightest light). The noise is never below ONE 8-bit grey level: on the
#     rig at 65 us the background read 2 counts nearly everywhere (MAD 0), the
#     old floor made the threshold 1.5 counts, and the faintly lit sample
#     (+2 counts) merged with the spot into one blob larger than max_area_px ->
#     "no blob" although the spot peaked at 200. The relative part keeps a
#     dim lit area or a scattered-light halo out of the spot at any exposure;
#   * min_area_px <= area <= max_area_px, not touching the frame edge
#     (reject_border), not reaching out of the region (it is looked at with a
#     margin: a blob that still touches the margin's edge goes on outside --
#     the rig's saturated illuminated block did, from the frame's top edge to
#     ~50 px from the laser), not elongated (the long / short axis ratio of
#     its pixels above max_elongation: a scratch or a lit edge is a line, a
#     spot is round, and a defocused ring is round too), and its centre inside
#     the region;
#   * (2026-09-29 late) the area limit is checked LAST and is automatic by
#     default (a quarter of the region, see AUTO_MAX_AREA_FRACTION): the
#     saturated laser with its rings is 3.4-7.8 k px^2 at the working
#     exposure. A compact blob holding the calibrated position that an
#     explicit limit rejects means "not found" with that reason, never a
#     piece of its rings; and a FRAGMENT (under FRAGMENT_ENERGY of the light
#     of a candidate at least about as bright) is never taken;
#   * of the candidates at least NEAR_FRACTION as bright (as large, for the
#     saturated calibration) as the best one, one whose bounding box HOLDS
#     the calibrated position wins, else the one NEAREST the calibrated
#     position -- not simply the brightest: at the working exposure the
#     laser and the lit sample are BOTH at full scale, and "brightest" was a
#     coin toss the block won (53 px off). With no calibration there is
#     nothing to be near, so the brightest (largest) wins, and two nearly equal
#     ones are refused (calibration) rather than guessed.


@dataclass
class SpotLocation:
    """Where the spot is for the size measurement (full-frame px)."""

    ok: bool = False
    x: float = float("nan")
    y: float = float("nan")
    method: str = "calibrated"
    why: str = ""                # when not ok: why nothing was located
    peak: float = 0.0            # smoothed peak above the background (counts)
    area: int = 0                # blob pixels (blob / calibration finders)
    n_candidates: int = 0
    # the runner-up, for the calibration's "several equally good" refusal
    second_xy: tuple | None = None
    second_peak: float = 0.0
    # blobs that were NOT taken for the spot, by reason ("reaches out of the
    # region": 1, ...) -- for the why-text and the Spot tab
    rejected: dict = field(default_factory=dict)


# of the spot-like candidates, those at least this fraction as bright (large)
# as the best one compete on distance to the calibration
NEAR_FRACTION = 0.5

# what to do when the region holds nothing usable
REGION_HINT = ("enlarge the search region (Spot tab) or set the position by hand "
               "(Set position / pick by clicking)")


def _grey_level(arr: np.ndarray, max_value=None) -> float:
    """ONE 8-bit grey level in this frame's counts: 1 on an 8-bit frame, 16 on
    a 12-bit one (4095 / 255). The floor under every noise estimate of the
    locate: a background that is quantised to (nearly) one value has a MAD of
    0, and a threshold of k x "0" cuts through anything the sample shows.
    Unknown full scale (float frames): the old 0.3-count floor."""
    top = _full_scale(arr, max_value)
    return 0.3 if top is None else float(top) / 255.0


def _search_area(shape, calib_xy, cfg):
    """(region or None, work box, region centre, centred on the frame centre?).

    The region = the search region around the calibrated laser position (the
    frame centre when there is none -- Lukas's rule, see above); None when
    lookup_region_px is 0 (the whole frame, the explicit opt-out). The WORK
    box is the region plus a margin, so that a candidate's whole shape is seen
    and one that goes on outside the region can be recognised as such."""
    h, w = shape[:2]
    half = int(_cfg(cfg, "lookup_region_px", 0) or 0)
    if half <= 0:
        return None, (0, 0, w, h), calib_xy, False
    at_centre = calib_xy is None
    centre = (w / 2.0, h / 2.0) if at_centre else (float(calib_xy[0]), float(calib_xy[1]))
    reg = search_region(centre, half, int(_cfg(cfg, "lookup_region_y_px", 0) or 0),
                        _cfg(cfg, "search_shape", "rect"), (w, h))
    if reg is None:                                  # the region lies outside the frame
        return None, (0, 0, 0, 0), centre, at_centre
    hx, hy = reg["half"]
    m = max(12, max(hx, hy) // 4)
    x0, y0, x1, y1 = reg["box"]
    return reg, (max(0, x0 - m), max(0, y0 - m), min(w, x1 + m), min(h, y1 + m)), \
        centre, at_centre


def _in_region(reg, x: float, y: float) -> bool:
    if reg is None:
        return True
    cx, cy = reg["center"]
    hx, hy = reg["half"]
    if reg["shape"] == "circle":
        return (x - cx) ** 2 + (y - cy) ** 2 <= hx * hx
    return abs(x - cx) <= hx + 0.5 and abs(y - cy) <= hy + 0.5


def _region_words(reg, centre, at_centre) -> str:
    """'inside the +/-100 px search region around the calibrated laser position (973, 465)'."""
    if reg is None:
        return "in the frame"
    hx, hy = reg["half"]
    size = (f"{hx} px radius" if reg["shape"] == "circle"
            else f"+/-{hx} px" if hx == hy else f"+/-{hx} x {hy} px")
    around = (f"the frame centre ({centre[0]:.0f}, {centre[1]:.0f}) -- there is no calibration "
              f"yet" if at_centre else
              f"the calibrated laser position ({centre[0]:.0f}, {centre[1]:.0f})")
    return f"inside the {size} search region around {around}"


def _rejected_words(rej: dict) -> str:
    parts = [f"{n} {why}" for why, n in rej.items() if n]
    return f" (ignored: {', '.join(parts)})" if parts else ""


def _candidates(mask: np.ndarray, vals: np.ndarray, thr, offset, full_size, cfg, reg=None,
                ref_xy=None, too_large: list | None = None):
    """The blobs of ``mask`` (in the work box at ``offset``) that may be the
    laser spot, by the rules above. ``vals`` = the signal the mask came from
    (its peak, the centroid's weights ``vals - thr``; ``thr`` None = every
    pixel weighs the same, the thresholded binary centroid).

    Returns (candidates, rejected counts, label image). A candidate is a dict:
    x, y (centroid, full frame), peak, area, energy (its light above the
    threshold -- or its summed values for a binary mask), label, bbox (in the
    work box), contains (its bounding box holds ``ref_xy``, the calibrated
    position).

    The AREA LIMIT is checked LAST (2026-09-29 late): a blob that passes every
    shape rule but is too large is still a compact spot-like light -- very
    likely the laser under a limit set too small -- and goes to ``too_large``
    (when a list is given) instead of just vanishing: the fragment rule
    compares against it and the why-text names it."""
    rej = {"smaller than min area": 0, "larger than max area": 0, "touching the frame edge": 0,
           "reaching out of the region": 0, "elongated": 0, "centred outside the region": 0}
    n, lab, stats, _c = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    out = []
    if n <= 1:
        return out, rej, lab
    mh, mw = mask.shape[:2]
    fw, fh = int(full_size[0]), int(full_size[1])
    ox, oy = int(offset[0]), int(offset[1])
    min_a = max(1, int(_cfg(cfg, "min_area_px", 4)))
    max_a = max_area_limit(cfg, reg)
    border = bool(_cfg(cfg, "reject_border", True))
    max_el = float(_cfg(cfg, "max_elongation", 3.0) or 0.0)
    for i in range(1, n):
        bx = int(stats[i, cv2.CC_STAT_LEFT])
        by = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_a:
            rej["smaller than min area"] += 1
            continue
        if border and (bx + ox <= 0 or by + oy <= 0 or bx + bw + ox >= fw or by + bh + oy >= fh):
            rej["touching the frame edge"] += 1
            continue
        # the work box's edge where it is NOT the frame's edge: a blob touching
        # it goes on outside what is looked at -- larger than the region, not
        # a spot inside it (the rig's illuminated block)
        if reg is not None and ((bx <= 0 < ox) or (by <= 0 < oy)
                                or (bx + bw >= mw and ox + mw < fw)
                                or (by + bh >= mh and oy + mh < fh)):
            rej["reaching out of the region"] += 1
            continue
        sub = lab[by:by + bh, bx:bx + bw] == i
        ys, xs = np.nonzero(sub)
        if max_el > 0 and area >= 5:
            # long / short axis of the pixel cloud; + 1/12 = a pixel's own
            # extent, so a 1-px-wide line reads long, not infinite
            ev = np.linalg.eigvalsh(np.cov(np.vstack([xs, ys]).astype(np.float64)))
            if math.sqrt((ev[1] + 1 / 12) / (max(ev[0], 0.0) + 1 / 12)) > max_el:
                rej["elongated"] += 1
                continue
        v = vals[by:by + bh, bx:bx + bw][sub].astype(np.float64)
        wgt = np.clip(v - thr, 0.0, None) if thr is not None else np.ones_like(v)
        energy = float(wgt.sum()) if thr is not None else float(v.sum())
        if wgt.sum() <= 0:
            wgt = np.ones_like(v)
        cx = float(xs @ wgt / wgt.sum()) + bx + ox
        cy = float(ys @ wgt / wgt.sum()) + by + oy
        if not _in_region(reg, cx, cy):
            rej["centred outside the region"] += 1
            continue
        contains = False
        if ref_xy is not None:
            rx, ry = int(round(ref_xy[0])) - ox, int(round(ref_xy[1])) - oy
            contains = bx <= rx < bx + bw and by <= ry < by + bh
        c = {"x": cx, "y": cy, "peak": float(v.max()), "area": area, "label": i,
             "bbox": (bx, by, bw, bh), "energy": energy, "contains": contains}
        if max_a > 0 and area > max_a:
            rej["larger than max area"] += 1
            if too_large is not None:
                too_large.append(c)
            continue
        out.append(c)
    return out, rej, lab


# A FRAGMENT is not the spot (2026-09-29 late, real frames at the working
# exposure): a piece of a ring, or a speck of dust lit next to the laser, is
# no brighter than the spot and carries a small fraction of its light. The
# rig's fragments carried 0.03-0.1 % of the laser's light (65-180 against
# 154 000), yet with the laser rejected by the area limit they WON (45-52 px
# off); and a saturated glint as bright at the top as the laser wins "nearest
# the calibration" whenever the calibration is a little stale. Rule: a
# candidate with less than FRAGMENT_ENERGY of the light of another compact
# candidate that is at least about as bright (peak >= FRAGMENT_PEAK x its own:
# two saturated tops differ only by noise) is a fragment of it and is never
# taken. Only against brighter-or-equal ones: a large DIM sample feature
# carries more light than a small bright spot and must not throw the spot out
# (the reason blob ranks by peak and not by energy).
FRAGMENT_ENERGY = 0.2
FRAGMENT_PEAK = 0.9
FRAGMENT_WORDS = "fragment (little light next to a brighter spot)"


def _drop_fragments(cands: list, too_large: list, rej: dict) -> list:
    """``cands`` without the fragments (see above); the reference lights are
    the candidates AND the compact blobs rejected only for their area."""
    refs = cands + too_large
    keep = []
    for c in cands:
        if any(o is not c and o["peak"] >= FRAGMENT_PEAK * c["peak"]
               and c["energy"] < FRAGMENT_ENERGY * o["energy"] for o in refs):
            rej[FRAGMENT_WORDS] = rej.get(FRAGMENT_WORDS, 0) + 1
        else:
            keep.append(c)
    return keep


def _too_large_words(big: dict, cfg, reg, at_calib: bool) -> str:
    """Why the spot was not taken: the area limit rejected it."""
    auto = AUTO_MAX_AREA_FRACTION * region_area(reg) if reg is not None else 0.0
    what = "the blob at the calibrated position" if at_calib else "the brightest spot-like blob"
    return (f"{what} ({big['x']:.0f}, {big['y']:.0f}) is {big['area']} px, larger than max "
            f"area {int(max_area_limit(cfg, reg))} px (Spot tab) -- set max area to 0 "
            f"(automatic: {auto:.0f} px, a quarter of the search region) or raise it")


def _pick(cands: list, too_large: list, key: str, ref_xy, cfg, reg, rej: dict):
    """(the candidate taken for the spot or None, the runner-up or None, why).

    1. A compact blob that holds the calibrated position (``ref_xy``) but was
       rejected by an explicit area limit: nothing is taken and the why says
       so -- whatever else is found then is a piece of the laser's rings.
    2. Fragments are dropped (_drop_fragments); fragments only -> nothing.
    3. _choose among the rest.
    ``why`` is "" when a candidate was taken, or when there was nothing."""
    held = [b for b in too_large if b["contains"]]
    if held:
        return None, None, _too_large_words(max(held, key=lambda b: b["energy"]), cfg, reg, True)
    cands = _drop_fragments(cands, too_large, rej)
    if not cands:
        if too_large:
            return None, None, _too_large_words(max(too_large, key=lambda b: b["energy"]),
                                                cfg, reg, False)
        return None, None, ""
    pick, second = _choose(cands, key, ref_xy)
    return pick, second, ""


def _choose(cands: list, key: str, ref_xy):
    """(the candidate taken for the spot, the runner-up or None) -- see the
    rules above: of those >= NEAR_FRACTION of the best ``key``, one HOLDING
    ``ref_xy`` in its bounding box (the one with the most light), else the
    nearest ``ref_xy``; the best one when there is no ``ref_xy``.

    Why "holds" before "nearest" (2026-09-29 late): the calibrated position
    inside a spot's own extent says it IS that spot, whatever the distances
    of the centroids -- the centroid of a defocused, lopsided saturated spot
    can sit further from a slightly stale calibration than a neighbour's."""
    order = sorted(cands, key=lambda c: -c[key])
    pick = order[0]
    if ref_xy is not None and len(order) > 1:
        close = [c for c in order if c[key] >= NEAR_FRACTION * order[0][key]]
        held = [c for c in close if c.get("contains")]
        if held:
            pick = max(held, key=lambda c: c.get("energy", 0.0))
        else:
            pick = min(close, key=lambda c: (c["x"] - ref_xy[0]) ** 2 + (c["y"] - ref_xy[1]) ** 2)
    others = [c for c in order if c is not pick]
    return pick, (others[0] if others else None)


def _plateau_peak(sm: np.ndarray, lab: np.ndarray, c: dict, noise: float) -> tuple:
    """The PEAK of candidate ``c`` (work-box px). A saturated spot has a flat
    top, and "the maximum" is then just its first pixel in raster order -- on
    the rig 14 px from the middle. So: the pixels within max(2 noise, 1 %) of
    the maximum; when they are a plateau (>= 9 px) their weighted centre,
    else the small centroid around the maximum (a peaked spot)."""
    bx, by, bw, bh = c["bbox"]
    sub = lab[by:by + bh, bx:bx + bw] == c["label"]
    v = np.where(sub, sm[by:by + bh, bx:bx + bw], -np.inf)
    m = float(v.max())
    base = m - max(2.0 * noise, 0.01 * abs(m))
    top = sub & (v >= base)
    ys, xs = np.nonzero(top)
    if xs.size >= 9:
        wgt = v[ys, xs].astype(np.float64) - base + 1e-9
        return float(xs @ wgt / wgt.sum()) + bx, float(ys @ wgt / wgt.sum()) + by
    iy, ix = np.unravel_index(int(np.argmax(v)), v.shape)
    return _peak_centroid(sm, int(ix) + bx, int(iy) + by)


def _peak_centroid(sig: np.ndarray, ix: int, iy: int, r: int = 3) -> tuple[float, float]:
    """Sub-pixel position of a maximum: the centroid of the (positive) signal in
    a (2r+1)^2 window around it."""
    h, w = sig.shape
    x0, x1 = max(0, ix - r), min(w, ix + r + 1)
    y0, y1 = max(0, iy - r), min(h, iy + r + 1)
    win = np.clip(sig[y0:y1, x0:x1].astype(np.float64), 0.0, None)
    tot = win.sum()
    if tot <= 0:
        return float(ix), float(iy)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    return float((xx * win).sum() / tot), float((yy * win).sum() / tot)


def _smooth_signal(gray: np.ndarray, box: tuple, cfg, max_value=None):
    """(smoothed signal of ``box``, its background, the noise of the smoothed
    copy). The box is MOSTLY background (a spot is small against the search
    region), so the robust level of all its pixels is the background; the
    smoothing (detect_px, >= 0.8 px) keeps one hot pixel from being "the
    brightest point". The noise is never below one 8-bit grey level
    (_grey_level): the rig's 65 us background was 2 counts nearly everywhere."""
    x0, y0, x1, y1 = box
    bright = bool(_cfg(cfg, "bright_spot", True))
    a = _signal(gray[y0:y1, x0:x1], bright, max_value).astype(np.float32)
    if a.size == 0:
        return a, 0.0, 0.0
    sig = max(0.8, float(_cfg(cfg, "detect_px", 2.0) or 0.0))
    sm = cv2.GaussianBlur(a, (0, 0), sig, borderType=cv2.BORDER_REFLECT)
    sub = sm[::2, ::2] if sm.size > 40000 else sm       # plenty of pixels for a median
    bg, noise = _dark_side_level(sub.ravel())
    return sm - bg, bg, max(noise, _grey_level(gray, max_value))


def _dark_side_level(vals: np.ndarray) -> tuple[float, float]:
    """(background, noise) of a box that may be partly covered by BRIGHT light.

    Rig 2026-09-29, working exposure: a saturated illuminated block covered a
    third of the box around a stale calibration. The median / MAD then moved
    up and the "noise" became the block's step: 5 x noise was above full
    scale and nothing was found. The dark side is what the background alone
    decides: the noise from the median down to the 15.9th percentile (one
    sigma of a Gaussian), everything brighter than median + 3 of those
    dropped, repeated until nothing more drops -- a bright part of up to
    ~half the box goes, the background's own statistics stay."""
    v = vals.astype(np.float64, copy=False)
    med = float(np.median(v))
    s = max(0.0, med - float(np.percentile(v, 15.87)))
    for _ in range(5):
        keep = vals[vals <= med + 3.0 * s + 1e-9]
        if keep.size < 16 or keep.size == v.size:
            break
        v = keep.astype(np.float64, copy=False)
        med = float(np.median(v))
        s = max(0.0, med - float(np.percentile(v, 15.87)))
    return med, s


def _region_peak(sm: np.ndarray, reg, work) -> float:
    """The brightest smoothed value inside the region's box (not the margin)."""
    if reg is None:
        return float(sm.max())
    x0, y0, x1, y1 = reg["box"]
    part = sm[y0 - work[1]:y1 - work[1], x0 - work[0]:x1 - work[0]]
    return float(part.max()) if part.size else float(sm.max())


def locate_spot(frame: np.ndarray, calib_xy, cfg=None, mode: str = "calibrated",
                max_value=None) -> SpotLocation:
    """The centre the spot SIZE is measured around (see the block comments above).

    ``calib_xy`` is the calibrated laser position: the search region is around
    it (None = not calibrated yet: around the frame centre) and, of several
    equally good candidates, the one nearest it is the laser. ``cfg`` = the
    Spot config (lookup_region_px / _y_px / search_shape, detect_px, locate_k,
    locate_rel, min/max_area_px, reject_border, max_elongation, bright_spot).
    Never raises: ``ok`` False + ``why`` instead.
    """
    mode = str(mode or "calibrated")
    if mode == "calibrated":
        if calib_xy is None:
            return SpotLocation(False, method=mode, why="no calibrated spot position")
        return SpotLocation(True, float(calib_xy[0]), float(calib_xy[1]), mode)
    gray = to_gray(frame) if frame.ndim == 3 else frame
    h, w = gray.shape[:2]
    reg, work, centre, at_centre = _search_area(gray.shape, calib_xy, cfg)
    out = SpotLocation(method=mode)
    sm, _bg, snoise = _smooth_signal(gray, work, cfg, max_value)
    if sm.size == 0:
        out.why = "the search region is outside the frame"
        return out
    where = _region_words(reg, centre, at_centre)
    hint = f" -- {REGION_HINT}" if reg is not None else ""
    k = max(1.0, float(_cfg(cfg, "locate_k", 5.0)))
    top = _region_peak(sm, reg, work)
    if top <= k * snoise:
        out.why = (f"no light above background + {k:g} x noise {where} (brightest "
                   f"{top:.1f} counts above the background, noise {snoise:.2f}){hint}")
        return out
    thr = max(k * snoise, float(_cfg(cfg, "locate_rel", 0.2) or 0.0) * top)
    big = []
    cands, rej, lab = _candidates(sm >= thr, sm, thr, (work[0], work[1]), (w, h), cfg, reg,
                                  calib_xy, big)
    pick, second, why = _pick(cands, big, "peak", calib_xy, cfg, reg, rej)
    out.rejected = rej
    if pick is None:
        out.why = (f"no spot {where}: {why}" if why else
                   f"no spot-like light {where}{_rejected_words(rej)}{hint}")
        return out
    out.n_candidates = len(cands)
    out.ok, out.peak, out.area = True, pick["peak"], pick["area"]
    if mode == "peak":
        px, py = _plateau_peak(sm, lab, pick, snoise)
        out.x, out.y = px + work[0], py + work[1]
    else:
        out.x, out.y = pick["x"], pick["y"]
    if second is not None:
        out.second_xy, out.second_peak = (second["x"], second["y"]), second["peak"]
    return out


# --------------------------------------------------------------------------- #
# Calibration: find the spot in the SAFETY AREA around the laser (2026-09-29)
# --------------------------------------------------------------------------- #
# The old calibration thresholded the whole frame and took the largest blob:
# right for a SATURATED spot (a flat top well above the illuminated sample),
# useless for an UNSATURATED one (a peaked spot with no flat top, dimmer than
# the threshold). Lukas: a switch --
#   "saturated"   -- the fixed threshold (thr_lower..thr_upper): the largest
#                    spot-like blob, its centroid (as before);
#   "unsaturated" -- the brightest spot-like blob of a smoothed copy, refined
#                    to the background-subtracted intensity centroid by the
#                    second moment's iterated box.
# At first (earlier on 2026-09-29) both searched the WHOLE frame, so that a
# stale calibration could not hide the spot. The rig showed what that costs: at
# the working exposure a saturated illuminated block won, frame after frame
# alternating with the laser (+-277 px). Lukas's rule since: "Always look for
# the laser spot in the safety area around the laser only!" -- the search
# region around the previous calibration (the frame centre for the very first
# one), the candidate rules above, and of several the one nearest the previous
# calibration. A spot outside the region is refused with the way out:
# enlarge the region, or set the position by hand.
# Without a previous calibration two nearly equal candidates (within
# AMBIGUOUS, 80 %) are refused: refusing is cheaper than calibrating on the
# wrong object -- the stabiliser would then steer the sample to it.
AMBIGUOUS = 0.8


def find_spot_for_calibration(frame: np.ndarray, cfg=None, mode: str = "saturated",
                              max_value=None, calib_xy=None) -> SpotLocation:
    """One frame's spot position for Calibrate spot (see above), searched in
    the region around ``calib_xy`` (the PREVIOUS calibration; None = the frame
    centre)."""
    gray = to_gray(frame) if frame.ndim == 3 else frame
    h, w = gray.shape[:2]
    bright = bool(_cfg(cfg, "bright_spot", True))
    out = SpotLocation(method=mode)
    reg, work, centre, at_centre = _search_area(gray.shape, calib_xy, cfg)
    x0, y0, x1, y1 = work
    if x1 <= x0 or y1 <= y0:
        out.why = f"the search region around ({centre[0]:.0f}, {centre[1]:.0f}) is outside the frame"
        return out
    where = _region_words(reg, centre, at_centre)
    hint = f" -- {REGION_HINT}" if reg is not None else ""
    if mode == "saturated":
        g8 = gray if gray.dtype == np.uint8 else to_gray(gray)
        g8 = g8[y0:y1, x0:x1]
        lo, hi = int(_cfg(cfg, "thr_lower", 200)), int(_cfg(cfg, "thr_upper", 255))
        if not bright:
            g8 = cv2.bitwise_not(g8)
            lo, hi = 255 - hi, 255 - lo
        mask = cv2.inRange(g8, lo, hi) > 0
        if not mask.any():
            out.why = (f"nothing above the threshold {lo} {where} (max there {int(g8.max())}) "
                       f"-- lower the threshold, calibrate as an 'unsaturated spot', or "
                       f"{REGION_HINT if reg is not None else 'check the laser'}")
            return out
        big = []
        cands, rej, lab = _candidates(mask, g8, None, (x0, y0), (w, h), cfg, reg, calib_xy, big)
        pick, second, why = _pick(cands, big, "area", calib_xy, cfg, reg, rej)
        out.rejected = rej
        if pick is None:
            out.why = (f"above the threshold {lo} {where}: {why}" if why else
                       f"light above the threshold {lo} {where}, but nothing spot-like"
                       f"{_rejected_words(rej)} -- check the area limits, or "
                       f"{REGION_HINT if reg is not None else 'the threshold'}")
            return out
        out.n_candidates = len(cands)
        out.x, out.y, out.area, out.peak = pick["x"], pick["y"], pick["area"], pick["peak"]
        out.ok = True
        if second is not None:
            out.second_xy, out.second_peak = (second["x"], second["y"]), float(second["area"])
            if calib_xy is None and second["area"] >= AMBIGUOUS * pick["area"]:
                out.ok = False
                out.why = (f"two blobs of nearly the same size above the threshold: "
                           f"{pick['area']} px at ({out.x:.0f}, {out.y:.0f}) and "
                           f"{second['area']} px at ({second['x']:.0f}, {second['y']:.0f}) "
                           f"-- raise the threshold or the min area, or set the position by hand")
        return out
    # "unsaturated": the brightest spot-like smoothed blob, then the moment centroid
    sm, _bg, snoise = _smooth_signal(gray, work, cfg, max_value)
    k = max(1.0, float(_cfg(cfg, "locate_k", 5.0)))
    top = _region_peak(sm, reg, work)
    if top <= k * snoise:
        out.why = (f"no light above background + {k:g} x noise {where} (brightest "
                   f"{top:.1f} counts above the background, noise {snoise:.2f}){hint}")
        return out
    thr = max(k * snoise, float(_cfg(cfg, "locate_rel", 0.2) or 0.0) * top)
    big = []
    cands, rej, _lab = _candidates(sm >= thr, sm, thr, (x0, y0), (w, h), cfg, reg, calib_xy, big)
    b, second, why = _pick(cands, big, "peak", calib_xy, cfg, reg, rej)
    out.rejected = rej
    if b is None:
        out.why = (f"no spot {where}: {why}" if why else
                   f"no spot-like light {where}{_rejected_words(rej)}{hint}")
        return out
    out.n_candidates = len(cands)
    out.peak, out.area = b["peak"], b["area"]
    if second is not None:
        out.second_xy, out.second_peak = (second["x"], second["y"]), second["peak"]
        if calib_xy is None and second["peak"] >= AMBIGUOUS * b["peak"]:
            out.why = (f"two spots of nearly the same brightness: {b['peak']:.0f} at "
                       f"({b['x']:.0f}, {b['y']:.0f}) and {second['peak']:.0f} at "
                       f"({second['x']:.0f}, {second['y']:.0f}) -- which one is the laser? "
                       f"(set the position by hand)")
            return out
    # refine: the second moment's iterated box around the chosen blob gives
    # the background-subtracted intensity centroid (rings, a hole: all
    # counted) -- but its box reaches whatever else is lit around the spot, and
    # a faintly lit sample next to it pulls that centroid over (12.5 px in a
    # copy of the rig's 65 us frame). The blob's own centroid (the part above
    # the threshold, weighted) is the position; the moment centroid replaces
    # it only when the two AGREE to within CALIB_REFINE_PX -- then it is the
    # same spot measured with more of its light, a little less noisy.
    m = spot_second_moment(frame, (b["x"], b["y"]), cfg, max_value=max_value)
    out.ok = True
    out.x, out.y = b["x"], b["y"]
    if m.ok and math.hypot(m.cx - b["x"], m.cy - b["y"]) <= CALIB_REFINE_PX:
        out.x, out.y = m.cx, m.cy
    return out


# see find_spot_for_calibration, "unsaturated": how far (px) the second
# moment's centroid may sit from the blob's before it is not trusted
CALIB_REFINE_PX = 1.0


# --------------------------------------------------------------------------- #
# More sizes: encircled energy, a Gaussian fit, the peak (2026-09-29)
# --------------------------------------------------------------------------- #
@dataclass
class SpotEncircled:
    """Radius holding ``fraction`` of the spot's background-subtracted energy."""

    ok: bool = False
    why: str = ""
    fraction: float = 0.86
    r_px: float = float("nan")   # r86 for fraction 0.86
    d_px: float = float("nan")   # D86 = 2 r86
    cx: float = float("nan")     # about the intensity centroid
    cy: float = float("nan")
    saturated: bool = False


def spot_encircled(frame: np.ndarray, guess_xy, cfg=None, max_value=None,
                   moments: SpotMoments | None = None) -> SpotEncircled:
    """Encircled energy: the radius about the intensity centroid that holds
    ``encircled_fraction`` (cfg, default 0.86) of the light above background.

    No threshold at all: rings and a central hole are simply energy at their
    radius, and the radius grows monotonically with defocus. The centroid, the
    background and the integration box are the second moment's (``moments``,
    computed here when not given): the box is 3 x D4sigma wide, which holds all
    but ~1e-8 of a Gaussian's energy. The total is the energy inside the
    largest circle that fits the box. For a Gaussian I ~ exp(-r^2 / 2 s^2) the
    enclosed fraction is 1 - exp(-r^2 / 2 s^2): r86 = s sqrt(-2 ln 0.14) = 1.98 s.
    A saturated spot reads too LARGE (its clipped core holds less light than it
    should) but still has its minimum near focus.
    """
    frac = min(max(float(_cfg(cfg, "encircled_fraction", 0.86)), 0.05), 0.995)
    out = SpotEncircled(fraction=frac)
    m = moments if moments is not None else spot_second_moment(frame, guess_xy, cfg, max_value)
    out.saturated = bool(m.saturated)
    if not m.ok:
        out.why = m.why or "no spot"
        return out
    gray = to_gray(frame) if frame.ndim == 3 else frame
    bright = bool(_cfg(cfg, "bright_spot", True))
    x0, y0, x1, y1 = m.box
    sig = _signal(gray[y0:y1, x0:x1], bright, max_value) - m.background
    yy, xx = np.mgrid[y0:y1, x0:x1]
    r = np.hypot(xx - m.cx, yy - m.cy).ravel()
    big_r = min(m.cx - x0, x1 - 1 - m.cx, m.cy - y0, y1 - 1 - m.cy)
    if big_r < 2:
        out.why = "integration box too small"
        return out
    inside = r <= big_r
    r, v = r[inside], sig.ravel()[inside].astype(np.float64)
    # E(r) must be a SMOOTH function of r, or r86 snaps to the radii of the
    # pixel rings (8 pixels at r = sqrt(145), ...): 1.2 % large on a sampled
    # Gaussian. So a pixel counts partly while the circle's edge crosses it --
    # linearly over one pixel width around its centre radius -- and the radius
    # holding the fraction is found by bisection (E rises with r; the noise
    # of negative pixels only wiggles it, the bisection still converges).
    total = float(v.sum())
    if total <= 0:
        out.why = "no energy above the background"
        return out
    goal = frac * total

    def enclosed(rad: float) -> float:
        return float(v @ np.clip(rad - r + 0.5, 0.0, 1.0))

    lo, hi = 0.0, float(big_r)
    for _ in range(30):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if enclosed(mid) < goal else (lo, mid)
    rr = 0.5 * (lo + hi)
    out.ok, out.r_px, out.d_px, out.cx, out.cy = True, rr, 2.0 * rr, m.cx, m.cy
    return out


@dataclass
class SpotGauss:
    """2-D Gaussian fit B + A exp(-(x-x0)^2/2sx^2 - (y-y0)^2/2sy^2)."""

    ok: bool = False
    why: str = ""
    x0: float = float("nan")
    y0: float = float("nan")
    sigma_x: float = float("nan")
    sigma_y: float = float("nan")
    sigma2: float = float("nan")     # (sx^2 + sy^2) / 2, px^2 -- like the moment's sigma^2
    amplitude: float = float("nan")
    offset: float = float("nan")
    r2: float = float("nan")         # fit quality: 1 - SSres / SStot
    saturated: bool = False          # clipped top: the fit is not usable then


def spot_gauss_fit(frame: np.ndarray, guess_xy, cfg=None, max_value=None,
                   moments: SpotMoments | None = None, max_half: int = 40,
                   iterations: int = 30) -> SpotGauss:
    """Fit an axis-aligned 2-D Gaussian to the spot (Levenberg-Marquardt, numpy).

    Started from the second moment (centroid, widths, background, peak) inside
    its integration box, capped at +-``max_half`` px around the centroid (the
    cost). Near focus a laser spot is close to Gaussian, so the fitted sigma^2
    is a size that the far tails and a noisy background barely move. Far out of
    focus a coherent spot has rings and a hole: the fit then describes it badly
    (``r2`` says so) and should not be trusted. A SATURATED spot has a flat top:
    fitted anyway, but flagged -- the readout shows "-- (saturated)".
    """
    out = SpotGauss()
    m = moments if moments is not None else spot_second_moment(frame, guess_xy, cfg, max_value)
    out.saturated = bool(m.saturated)
    if not m.ok:
        out.why = m.why or "no spot"
        return out
    gray = to_gray(frame) if frame.ndim == 3 else frame
    bright = bool(_cfg(cfg, "bright_spot", True))
    h, w = gray.shape[:2]
    bx0, by0, bx1, by1 = m.box
    cxi, cyi = int(round(m.cx)), int(round(m.cy))
    x0, x1 = max(bx0, cxi - max_half, 0), min(bx1, cxi + max_half + 1, w)
    y0, y1 = max(by0, cyi - max_half, 0), min(by1, cyi + max_half + 1, h)
    if x1 - x0 < 5 or y1 - y0 < 5:
        out.why = "fit region too small"
        return out
    z = _signal(gray[y0:y1, x0:x1], bright, max_value).astype(np.float64).ravel()
    yy, xx = np.mgrid[y0:y1, x0:x1]
    xx, yy = xx.ravel().astype(np.float64), yy.ravel().astype(np.float64)
    p = np.array([m.cx, m.cy, math.sqrt(max(m.sigma2_x, 0.25)),
                  math.sqrt(max(m.sigma2_y, 0.25)), max(m.peak, 1.0), m.background])
    lam = 1e-3

    def model(q):
        g = np.exp(-((xx - q[0]) ** 2 / (2 * q[2] ** 2) + (yy - q[1]) ** 2 / (2 * q[3] ** 2)))
        return q[5] + q[4] * g, g

    f, g = model(p)
    cost = float(((z - f) ** 2).sum())
    jac = np.empty((z.size, 6))
    for _ in range(max(1, int(iterations))):
        dx, dy = xx - p[0], yy - p[1]
        ag = p[4] * g
        jac[:, 0] = ag * dx / p[2] ** 2
        jac[:, 1] = ag * dy / p[3] ** 2
        jac[:, 2] = ag * dx * dx / p[2] ** 3
        jac[:, 3] = ag * dy * dy / p[3] ** 3
        jac[:, 4] = g
        jac[:, 5] = 1.0
        a = jac.T @ jac
        b = jac.T @ (z - f)
        try:
            step = np.linalg.solve(a + lam * np.diag(np.diag(a) + 1e-12), b)
        except np.linalg.LinAlgError:
            break
        q = p + step
        q[2], q[3] = abs(q[2]) + 1e-6, abs(q[3]) + 1e-6
        fq, gq = model(q)
        cq = float(((z - fq) ** 2).sum())
        if cq < cost:
            p, f, g, cost, lam = q, fq, gq, cq, lam * 0.3
            if np.abs(step[:4]).max() < 1e-4:
                break
        else:
            lam *= 10.0
            if lam > 1e8:
                break
    sst = float(((z - z.mean()) ** 2).sum())
    out.x0, out.y0 = float(p[0]), float(p[1])
    out.sigma_x, out.sigma_y = float(p[2]), float(p[3])
    out.sigma2 = float(0.5 * (p[2] ** 2 + p[3] ** 2))
    out.amplitude, out.offset = float(p[4]), float(p[5])
    out.r2 = 1.0 - cost / sst if sst > 0 else float("nan")
    if not (x0 <= p[0] < x1 and y0 <= p[1] < y1) or p[4] <= 0:
        out.why = "the fit left the spot (no Gaussian here)"
        return out
    out.ok = True
    return out


def saturation_info(frame: np.ndarray, guess_xy, cfg=None, max_value=None,
                    target: float = 0.8) -> tuple[float, float]:
    """(fraction of the spot's pixels at full scale, exposure factor that would
    bring its peak to ``target`` x full scale) -- the factor is an ESTIMATE.

    The spot's pixels = those above rel_level of the way from the background
    to full scale. A saturated peak is unknown, but for a Gaussian-like spot
    the areas above two levels tell it: the area above a level L is
    2 pi s^2 ln(P / L), so with A_top at full scale and A_half above half of it
    (both counted from the background), 2 pi s^2 = (A_half - A_top) / ln 2 and
    P = top exp(A_top ln 2 / (A_half - A_top)). Unsaturated: target x top /
    peak. (NaN, NaN) when nothing is measurable.
    """
    gray = to_gray(frame) if frame.ndim == 3 else frame
    top = _full_scale(gray, max_value)
    if top is None:
        return float("nan"), float("nan")
    x0, y0, x1, y1 = _limit_box(gray.shape, guess_xy, cfg)
    crop = gray[y0:y1, x0:x1].astype(np.float64)
    if crop.size == 0:
        return float("nan"), float("nan")
    bg, _n = _robust_level(crop.ravel())
    rel = min(max(float(_cfg(cfg, "rel_level", 0.135)), 0.001), 0.999)
    n_spot = int(np.count_nonzero(crop >= bg + rel * (top - bg)))
    a_top = int(np.count_nonzero(crop >= top))
    if n_spot == 0:
        return float("nan"), float("nan")
    frac = a_top / n_spot
    if a_top == 0:
        peak = float(crop.max()) - bg
        return 0.0, ((target * top - bg) / peak if peak > 0 else float("nan"))
    a_half = int(np.count_nonzero(crop >= bg + 0.5 * (top - bg)))
    if a_half <= a_top:
        return frac, float("nan")
    p_est = (top - bg) * math.exp(a_top * math.log(2.0) / (a_half - a_top))
    return frac, (target * top - bg) / p_est


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


# Metrics whose curve through focus IS a parabola in Z (not just near the
# bottom): the second moment sigma^2 of any coherent beam, w^2(z) = w0^2 +
# c (z - z0)^2 (ISO 11146). Only for these may the one_way park trust the
# fitted curvature far enough to step from the edge of the park band to its
# centre (Camera._af_one_way, park_centre).
PARABOLIC_FOCUS = frozenset({"spot_d4sigma"})


@dataclass
class FocusTarget:
    """What the one_way park aims for, from the fine walk (robust_focus_target).

    value      -- the metric at best focus (fitted extremum / running mean / level)
    z          -- where the fine walk put it (Z counter units)
    noise      -- the level-to-level scatter of the metric (absolute, 0 = unknown)
    curvature  -- |a| of the fitted parabola, metric per Z unit^2 (0 = no fit)
    n          -- levels used; n_dropped -- outlying levels left out of the fit
    method     -- "parabola" | "running mean" | "single level" | "none"
    """

    value: float
    z: float
    noise: float = 0.0
    curvature: float = 0.0
    n: int = 0
    n_dropped: int = 0
    method: str = "none"


def robust_focus_target(z_levels, metrics, maximise: bool = False,
                        rel_window: float | None = None, half_window: int = 3,
                        repeat_noise: float = 0.0) -> FocusTarget:
    """The fine walk's best focus metric, robust against ONE noisy level.

    Why (rig, 2026-09-29): the one_way park used to aim at the single best
    level of the fine walk. The lowest of ~10 noisy levels is biased low by
    construction, and on the rig it was a dip -- 51.2 px^2 among neighbours
    of 56.2 / 55.9 -- so "within 4 % of it" (<= 53.2) was below anything the
    camera could read again at focus, and the park failed.

    Here: a parabola fitted to the levels around the best one (every level
    within ``rel_window`` x the minimum when that is wider, as the sweep's fit
    does for sigma^2), ONE pass of outlier rejection (a level more than 3
    robust sigmas off the curve is dropped and the fit repeated -- that is
    exactly the rig's dip), and the FITTED extremum is the target. For sigma^2
    the parabola is the physics, so this is the right number, not just a
    smoother one. The scatter of the kept levels about the curve is the noise
    per level (``repeat_noise``, the frame-to-frame error of one level mean,
    is used when it is larger or when there is no fit).

    No usable parabola (fewer than 5 levels, opens the wrong way, vertex far
    outside the levels, or a vertex that dives below the levels it was fitted
    to -- a kinked curve, not a parabola): the best 3-level RUNNING MEAN instead (a lone dip is
    diluted 3x), noise from the second differences of the walk. One level:
    that level. The metric's own sense is kept (``maximise``: the fitted
    MAXIMUM of edges / fft).
    """
    z = np.asarray(z_levels, dtype=float).ravel()
    m = np.asarray(metrics, dtype=float).ravel()
    ok = np.isfinite(z) & np.isfinite(m)
    z, m = z[ok], m[ok]
    rep = float(repeat_noise) if np.isfinite(repeat_noise) and repeat_noise > 0 else 0.0
    if z.size == 0:
        return FocusTarget(float("nan"), float("nan"), method="none")
    sgn = -1.0 if maximise else 1.0
    s = sgn * m                       # "score": smaller is better, whatever the metric
    idx = int(np.argmin(s))

    # ---- parabola over the levels around the best ------------------------
    lo, hi = max(0, idx - half_window), min(z.size, idx + half_window + 1)
    if rel_window and not maximise and m[idx] > 0:
        lo2, hi2 = idx, idx + 1       # the contiguous run within the window
        while lo2 > 0 and m[lo2 - 1] <= rel_window * m[idx]:
            lo2 -= 1
        while hi2 < z.size and m[hi2] <= rel_window * m[idx]:
            hi2 += 1
        if hi2 - lo2 > hi - lo:
            lo, hi = lo2, hi2
    zz, ss = z[lo:hi], s[lo:hi]
    keep = np.ones(zz.size, dtype=bool)

    def fit(mask):
        if mask.sum() < 5:            # 3 parameters + at least 2 to judge the noise
            return None
        try:
            return np.polyfit(zz[mask], ss[mask], 2)
        except Exception:
            return None

    coef = fit(keep)
    if coef is not None and zz.size >= 6:
        r = ss - np.polyval(coef, zz)
        mad = 1.4826 * float(np.median(np.abs(r)))
        out = np.abs(r) > 3.0 * mad if mad > 0 else np.zeros_like(keep)
        # only a FEW levels may go: more "outliers" than that means the curve,
        # not the levels, is wrong -- then keep them all and let the checks decide
        if 0 < out.sum() <= max(1, zz.size // 4):
            keep = ~out
            coef = fit(keep)
    if coef is not None:
        a, b, c = (float(v) for v in coef)
        step = float(np.median(np.abs(np.diff(zz)))) if zz.size > 1 else 0.0
        if a > 1e-12:                 # convex in the score = a real extremum
            zv = -b / (2 * a)
            res = ss[keep] - np.polyval(coef, zz[keep])
            noise = float(np.sqrt(np.sum(res ** 2) / max(1, keep.sum() - 3)))
            vertex = c - b * b / (4 * a)
            # IS it a parabola? A parabola forced onto a KINK (the relative
            # area, a Gaussian fit's sigma^2 on the coherent spot: flat, then
            # a jump where the ring takes over) fits badly and its vertex dives
            # below every level -- sim: 190 against a best level of 228 -- so
            # no park could reach it. The judge is the noise seen LOCALLY,
            # from neighbouring levels (second differences, median: a kink or
            # a dip is one outlier there): a real parabola leaves residuals of
            # that size and a vertex within a few of them of its best level.
            best_kept = float(ss[keep].min())
            sk = ss[keep]
            local = (1.4826 * float(np.median(np.abs(np.diff(sk, 2)))) / np.sqrt(6.0)
                     if sk.size >= 5 else noise)
            local = max(local, rep, 0.002 * abs(best_kept), 1e-12)
            plausible = (noise <= 3.0 * local
                         and vertex >= best_kept - max(3.0 * local, 0.01 * abs(best_kept)))
            if zz.min() - step <= zv <= zz.max() + step and plausible:
                return FocusTarget(float(sgn * vertex), float(zv), max(noise, rep), a,
                                   int(keep.sum()), int((~keep).sum()), "parabola")

    # ---- fallback: the best 3-level running mean -------------------------
    if z.size >= 3:
        run = np.convolve(s, np.ones(3) / 3.0, mode="valid")    # centred on 1..n-2
        j = int(np.argmin(run))
        noise = rep
        if z.size >= 5:
            # a smooth curve has small second differences; noise does not:
            # var(m[i-1] - 2 m[i] + m[i+1]) = 6 sigma^2 (median: one dip ignored)
            d2 = np.diff(m, 2)
            noise = max(noise, 1.4826 * float(np.median(np.abs(d2))) / np.sqrt(6.0))
        return FocusTarget(float(sgn * run[j]), float(z[j + 1]), noise, 0.0, 3, 0,
                           "running mean")
    return FocusTarget(float(m[idx]), float(z[idx]), rep, 0.0, 1, 0, "single level")


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
