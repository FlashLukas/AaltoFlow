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
FOCUS_MAXIMISE = {"spot_area": False, "edges": True, "fft": True}


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

    # default: spot_area
    mask = cv2.inRange(gray, int(spot_thr[0]), int(spot_thr[1]))
    return float(cv2.countNonZero(mask))


def focus_is_maximised(mechanism: str) -> bool:
    return FOCUS_MAXIMISE.get(mechanism, True)


def best_focus_from_sweep(
    z_levels, metrics, maximise: bool = True, fit_curve: bool = True
) -> float:
    """Given a Z sweep, return the Z of best focus.

    ``fit_curve`` fits a parabola near the extremum and returns its vertex
    (sub-step precision); otherwise the raw argmax/argmin Z is returned.
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
