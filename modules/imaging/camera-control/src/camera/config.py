"""Configuration for the camera vision brain (blueprint §4).

Every tunable number lives on a small ``@dataclass`` grouped by concern, and the
whole thing is persisted as a plain-text INI via :mod:`configparser`.  The
groups deliberately mirror the tabs of the original LabVIEW front panel so the
port is easy to check against the instrument you already know:

    Camera settings tab   -> Camera, Image
    AutoFocus settings    -> Spot, Autofocus
    Pattern matching tab  -> Pattern
    Define scanning tab    -> Scanning
    (stabiliser controls) -> Stabilizer
    Positioner settings   -> Limits, Hardware

WHY dataclasses + INI (and not a big dict/JSON)?  Attribute access with editor
autocomplete and a clear typed "here is exactly what is configurable" contract,
plus a human-readable file you can hand-edit in the lab without opening Python.

Coordinate conventions used across the whole package:
  * Image/pixel coordinates: x to the right, y DOWN (OpenCV convention).
  * Stage coordinates: micrometres (um).  Pixel <-> um uses the per-objective
    ``pixel_size_x_um`` / ``pixel_size_y_um`` (see objectives.py).
"""

# `from __future__ import annotations` makes every annotation a *string* at
# runtime -- which is exactly why the INI loader routes each value through
# `_cast(raw, field.type)` using the type NAME.  See load_config().
from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, fields

# The focus-scoring mechanisms (Autofocus.mechanism). spot_area = thresholded
# area (fixed threshold); spot_d4sigma = the spot's second moment sigma^2 (no
# threshold, a parabola in Z for a coherent beam); spot_relative = the area
# above a fraction of the spot's own peak. See vision.py "Spot SIZE".
FOCUS_MECHANISMS = ("spot_area", "edges", "fft", "spot_d4sigma", "spot_relative")
# Autofocus routines (Autofocus.routine): the symmetric sweep, or the one-way
# walk made for a hysteretic (slip-stick) Z -- see Camera._af_one_way.
AF_ROUTINES = ("sweep", "one_way")
# Which side the one-way routine approaches focus from (Autofocus.approach_from):
# "below" walks Z upwards, "above" walks it downwards.
AF_SIDES = ("below", "above")
# How the image is mirrored after rotation (Image.symmetry).
SYMMETRIES = ("none", "horizontal", "vertical")
# GUI theme choices (UI.theme).
THEMES = ("dark", "light")
# Which rig drives the sample (Hardware.motion).
MOTIONS = ("kim", "piezo")
# Spot search region shape (Spot.search_shape).
SEARCH_SHAPES = ("rect", "circle")
# How the spot SIZE is reported as "the" size (Spot.size_method): the fixed
# threshold (as always), relative to the spot's own peak, or the second moment.
SIZE_METHODS = ("threshold", "relative", "d4sigma")
# Which pixels the second moment counts (Spot.clip_mode), see vision.spot_second_moment.
CLIP_MODES = ("local", "pixel")
# The simulator's laser spot (Camera.sim_spot_model).
SIM_SPOTS = ("gaussian", "coherent")
# Pixel depths the simulated camera can deliver (Camera.sim_bit_depth).
SIM_BIT_DEPTHS = (8, 10, 12, 16)
# XY position / jog unit on a stage that counts steps (Hardware.xy_unit).
XY_UNITS = ("steps", "um")


# --------------------------------------------------------------------------- #
# Config groups (one @dataclass per concern)
# --------------------------------------------------------------------------- #
@dataclass
class Camera:
    """Acquisition parameters (LabVIEW 'Camera settings')."""

    camera_name: str = "cam1"          # device serial / friendly name ("" = first)
    driver: str = "ids"                # real backend: "ids" | "genicam"
    video_mode: str = "Mono8 640x480"  # free text; real driver validates
    frame_rate: float = 15.0           # frames/s the engine tries to hold
    extra_delay_ms: float = 0.0        # optional extra pause per frame
    running_avg_frames: int = 1        # rolling temporal average (1 = off)
    # The camera's exposure as last READ from it. Since 2026-09-27 (adopt rule)
    # it is NEVER pushed at start: start() replaces it with the camera's own
    # ExposureTime. It is written to the camera only when the user changes it
    # (live parameter panel, or set_config with a different value). 0 = unknown.
    exposure_us: float = 0.0
    # SIMULATOR only: the laser spot the simulated camera draws. "gaussian" =
    # the old toy (grows with defocus at full brightness); "coherent" = rings
    # and a central hole through focus, power conserved (backends/sim.py
    # CoherentSpot) -- to try the threshold-free spot sizes without the rig.
    sim_spot_model: str = "gaussian"
    # SIMULATOR only: 8 = the old 8-bit frames; 12 = the simulated camera also
    # delivers a 12-bit copy of every frame (like the lab camera in Mono12),
    # which the spot SIZE metrics then use -- see backends/sim.py.
    sim_bit_depth: int = 8


@dataclass
class Image:
    """Geometry + pixel calibration applied to every grabbed frame.

    ``pixel_size_*_um`` is normally filled from the objective table
    (objectives.py) but is stored here so a loaded config is self-contained.
    """

    save_path: str = "captures"        # folder for SavePicture (auto-suffixed)
    objectives_file: str = "objectives.ini"  # table of objective -> pixel size
    objective_name: str = "20x - Zeiss NA 0.7"
    pixel_size_x_um: float = 0.4130    # um per pixel (X); from the objective
    pixel_size_y_um: float = 0.4130    # um per pixel (Y)
    rotation_deg: float = 0.0          # rotate the raw frame to align stage axes
    symmetry: str = "none"             # none | horizontal | vertical mirror
    clip_enabled: bool = False         # crop to a rectangle before processing
    clip_left: int = 0
    clip_top: int = 0
    clip_right: int = 0                # 0 = full width (see brain preprocess)
    clip_bottom: int = 0               # 0 = full height


@dataclass
class Spot:
    """Laser-spot detection (threshold -> largest blob -> centre of mass)."""

    thr_lower: int = 200               # keep pixels in [thr_lower, thr_upper]
    thr_upper: int = 255
    bright_spot: bool = True           # True: bright spot on dark background
    # Per-frame size check: search only a region around the CALIBRATED spot
    # (uncalibrated, or lookup_region_px = 0: the whole frame).
    search_shape: str = "rect"         # "rect" | "circle"
    lookup_region_px: int = 100        # rect: half-width (+/- px in x); circle: radius
    lookup_region_y_px: int = 0        # rect: half-height (+/- px in y); 0 = same as x
    min_area_px: int = 4               # ignore blobs smaller than this
    max_area_px: int = 20000           # ...and larger than this (0 = no limit): a
                                       #   saturated illumination patch is not a spot
    reject_border: bool = True         # ignore blobs touching the frame edge
    # per-frame check only: count just the bright pixels that are symmetric
    # about the calibrated centre, so another object reaching into the search
    # region is neither taken for the spot nor added to its area
    symmetric: bool = True

    # CALIBRATED spot position (Spot tab -> "Calibrate spot": the centroid
    # averaged over N frames, searched over the whole frame). The laser spot is
    # FIXED in the image -- the sample moves under it -- so its position is a
    # user decision, and THIS is the position click-to-go and the stabiliser use
    # (decided with Lukas 2026-09-13). Each frame still thresholds a small box
    # around it, but only to evaluate the spot's size. Recalibrate after
    # realigning the beam.
    ref_set: bool = False
    ref_x: float = 0.0                 # px, processed-frame coordinates
    ref_y: float = 0.0
    ref_area: float = 0.0              # px^2 at calibration time
    ref_jitter_px: float = 0.0         # std of the centroid while calibrating
    # ... and the other two sizes at calibration time (0 = not measured)
    ref_d4sigma_px: float = 0.0        # D4sigma = 4 sqrt(sigma^2), px
    ref_rel_area: float = 0.0          # area above rel_level x peak, px^2

    # SPOT SIZE WITHOUT A FIXED THRESHOLD (2026-09-28, Lukas: a defocused
    # coherent spot has rings and a hole; a fixed threshold loses it). All
    # three sizes are measured every frame in the search region; size_method
    # says which one the Spot tab plots as "the" size. The autofocus picks its
    # own (autofocus.mechanism). "threshold" keeps everything as it was.
    size_method: str = "threshold"     # threshold | relative | d4sigma
    # relative: pixels above rel_level x (peak - background). 0.135 = 1/e^2.
    rel_level: float = 0.135
    # d4sigma (second moment, ISO 11146): background from a ring outside the
    # box; a pixel counts when the image blurred by detect_px is above
    # clip_sigma x ITS noise ("local"; "pixel" = the textbook per-pixel clip,
    # biased low for a dim spot); clumps under min_blob_px are dropped and the
    # mask grown by mask_grow_px (a kept noise pixel adds scatter, a dropped
    # wing pixel adds bias). The box is box_factor x D4sigma each way from the
    # centroid (1.5 = 3 x D4sigma wide), iterated up to max_iter times.
    clip_sigma: float = 3.0
    clip_mode: str = "local"           # local | pixel
    detect_px: float = 2.0
    min_blob_px: int = 20
    mask_grow_px: int = 4
    box_factor: float = 1.5
    max_iter: int = 10
    # Gaussian blur of the VALUES before the moments (px, 0 = off). Its own
    # smooth_px^2 is subtracted again, so it cannot move the focus.
    smooth_px: float = 0.0
    # both threshold-free sizes: drop every clump of light that has no twin
    # on the opposite side of the calibrated centre -- a feature of the sample
    # next to the spot. The spot (round, elliptical, rings) always has one.
    reject_asymmetric: bool = True


@dataclass
class Pattern:
    """Template pattern matching (OpenCV, ports NI 'Grayscale Value Pyramid')."""

    n_matches: int = 1                 # matches requested (we use the best)
    min_match_score: float = 0.6       # 0..1 normalised cross-correlation floor
    angle_start: float = 0.0           # rotation search range, degrees
    angle_end: float = 0.0             # 0..0 = no rotation search (fastest)
    angle_step: float = 2.0            # step when a range is given
    safety_area_px: int = 100          # size of the safe search box around match
    full_image: bool = False           # search the whole frame (ignore box)
    # Backup patterns (Pattern card -> "Draw backup ROI"). The driving pattern
    # hands over when it comes closer than edge_margin_px to the frame edge (or
    # is lost) to the matched pattern with the most room. While two are in view
    # their offset is refined by offset_learn_rate per frame (0 = never), unless
    # they disagree by more than offset_warn_px -- then it is a bad match and
    # nothing is learnt.
    edge_margin_px: int = 60
    offset_learn_rate: float = 0.05
    offset_warn_px: float = 15.0
    # LOSING THE PATTERN (2026-09-28, Lukas: "if it is lost it is out of focus,
    # out of image, the spot is in the pattern, or something else happened that
    # is terrible. All need correction."). Not matched for `lost_frames` frames
    # in a row = LOST: the camera raises a latched `fault` (a scan pauses on it)
    # naming the likely cause, and the stabiliser holds until "Clear fault".
    # The cause is read from where the pattern was LAST seen: within
    # loss_edge_margin_px of the image edge -> out of image; within
    # loss_spot_margin_px of the laser spot -> the spot is on the pattern;
    # otherwise out of focus (or unknown). There is deliberately NO search of
    # the whole frame after a loss -- it can lock onto a look-alike feature.
    lost_frames: int = 5
    loss_edge_margin_px: int = 40
    loss_spot_margin_px: int = 30
    # Only for the "out of focus / unknown" case: run ONE autofocus, then look
    # for the pattern again at its last place. Found -> fault cleared (a warning
    # is logged); not found -> the fault latches. Off by default.
    autofocus_on_loss: bool = False
    # ... but at most one recovery autofocus per this many seconds: a pattern
    # lost AGAIN sooner latches the fault at once (a flickering pattern must
    # not keep the Z walking).
    recovery_min_interval_s: float = 300.0


@dataclass
class Autofocus:
    """Focus sweep + continuous focus (LabVIEW 'AutoFocus settings')."""

    # spot_area | edges | fft | spot_d4sigma | spot_relative. NB spot_area is
    # MINIMISED, which assumes a SATURATED spot (its thresholded area only
    # grows with defocus); an unsaturated spot's area is largest at focus --
    # the brain warns (af_hint), use spot_d4sigma for such a spot.
    mechanism: str = "spot_area"
    focus_from_safety_area: bool = False  # score the safety box, not full frame
    drive_amplitude_v: float = 6.0     # peak-to-peak Z sweep, volts
    steps: int = 21                    # focus levels per sweep
    averages_per_level: int = 3        # frames averaged at each Z
    offset_from_found_v: float = 0.0   # park this far from the found best focus
    fit_curve: bool = True             # True: parabola fit; False: raw argmax
    continuous_enabled: bool = False   # hold focus every frame
    # NB (2026-09-28): continuous_gain is used as the Z step per frame, in the
    # Z unit (V / um) -- NOT a fraction; continuous_target is not used at all.
    # Kept as they are until Lukas decides (camera CLAUDE.local.md).
    continuous_gain: float = 0.2       # Z dither step per frame, Z unit
    continuous_target: float = 0.0     # (unused) 0 = hold the last measured focus metric
    # Open-loop Z only (KIM / PIA25). A slip-stick step is not the same size up
    # as down, so a move that REVERSES direction lands off target. Every sweep
    # level and the final park are therefore approached from BELOW: first go
    # this far under the target, then up to it. In the Z device's own unit
    # (um for KIM). Ignored for closed-loop / voltage-driven Z.
    approach_margin: float = 1.0
    # --- routine "one_way" (2026-09-24, Lukáš: the sweep is unreliable on a
    # hysteretic piezo). Every level that COUNTS is reached moving the same
    # way, and the final park is decided by the IMAGE, not the step counter.
    #   1. coarse: follow the slope of the metric (bigger steps while far out of
    #      focus; for spot_area aimed by extrapolating the spot radius) until
    #      it gets worse -> the minimum is bracketed;
    #   2. fine: from the approach side, walk through it in fine_step_v until
    #      the metric has been worse than the best for rise_levels levels;
    #   3. park: back off past the best, walk in again, stop at the first level
    #      within park_tolerance of the best metric.
    # Step sizes are in the Z device's unit (um on kim), like the fields above.
    routine: str = "sweep"             # sweep | one_way
    approach_from: str = "below"       # below (walk Z up) | above (walk Z down)
    coarse_step_v: float = 2.0         # first coarse step; grows while far away
    fine_step_v: float = 0.25          # step of the fine walk and the park walk
    max_travel_v: float = 40.0         # coarse search: max distance from the start
    rise_fraction: float = 0.10        # "worse" = worse than the best by this fraction
    rise_levels: int = 2               # ...for this many levels in a row -> stop
    # Park where the metric is within this FRACTION of the fine walk's best.
    # One number does not suit every metric (rig test 2026-09-28): an area or
    # an edge score changes ~linearly near focus, but sigma^2 is a PARABOLA --
    # flat at the bottom -- so 10 % of sigma^2 is already |dz| ~0.3 Rayleigh
    # ranges from focus, and the park stopped that early on the approach side.
    # So: park_tolerance is the value for spot_area / edges / fft (and the
    # fallback); the two threshold-free sizes have their own, 0.04 from the
    # rig (14/15 parks within ~0.2 focal depth, std < 1 %). Set one of them to
    # 0 to use park_tolerance for it again. Camera.park_tolerance() picks.
    park_tolerance: float = 0.10
    park_tolerance_d4sigma: float = 0.04
    park_tolerance_relative: float = 0.04
    # How long a SCAN waits for one autofocus before calling it failed (s).
    # Generous: an open-loop Z walks slowly and a far-off start takes many levels.
    scan_timeout_s: float = 600.0
    # --- Z STEP CALIBRATION by the camera (2026-09-28, Camera.calibrate_z_steps)
    # A slip-stick Z steps UP and DOWN by different amounts, so its counter is
    # a poor ruler. The routine walks Z up through focus, then down through it,
    # in equal COUNTER steps, measuring sigma^2 (D4sigma) at every level, and
    # fits a parabola per direction: the ratio of the two curvatures is
    # (step up / step down)^2. Distances in the Z unit (um on kim), BY THE
    # COUNTER (the routine does not trust them -- that is the point).
    zcal_step_v: float = 0.25          # counter distance between two levels
    zcal_start_offset_v: float = 3.0   # the up walk starts this far below the current Z
    zcal_max_travel_v: float = 30.0    # give up if one walk goes further than this
    zcal_averages: int = 3             # frames averaged per level
    # fit only the levels with sigma^2 <= this x the smallest one (and the
    # walks end there): far out the faint wings drop below the camera's grey
    # levels and sigma^2 reads LOW (rig: R^2 0.98-0.997 within 3x, the wings up
    # to 50 % low beyond; simulator, 8 bit: already 15 % low at 3x on one side)
    # -- 2 is the sweep's window too. With 12-bit frames 3 is worth a try.
    zcal_fit_window: float = 2.0
    zcal_min_r2: float = 0.97          # refuse (write nothing) below this, per walk
    zcal_min_side_levels: int = 3      # fitted levels needed on EACH side of the minimum
    # The absolute scale is not measurable this way (only the ratio). 0 = keep
    # the geometric mean of the two step sizes at the stage's current Z step;
    # > 0 = a known step (um) to use as that mean instead.
    zcal_step_um: float = 0.0


@dataclass
class Scanning:
    """The scanning-point array, defined relative to the template (LabVIEW
    'Define scanning').  Points are laid out on a grid, rotated by
    ``angle_deg``, and pinned to the template centre."""

    points_x: int = 3
    points_y: int = 3
    dx_um: float = 1.0                 # grid pitch in X, um
    dy_um: float = 1.0                 # grid pitch in Y, um
    angle_deg: float = 0.0             # rotate the whole array
    selected_index_x: int = 0          # which point the stabiliser targets
    selected_index_y: int = 0
    overlay_size: int = 5              # marker radius, px (GUI overlay)
    overlay_style: str = "fill"        # fill | open


@dataclass
class Stabilizer:
    """Drift stabiliser (ports Stabilization_StableAtPixelOrCorrectV2.vi).

    The spot->selected-point distance is averaged over ``images_to_average``
    frames; if the mean lies outside ``stable_radius_um`` the stage moves by
    ``gain`` x that error, then waits ``settle_s`` (and, on an open-loop stage,
    until it stops) before measuring again. Inside the radius: Stable, no move.

    2026-09-14 (Lukáš: "make it faster, more bold, and define the distance that
    counts as stable"): LabVIEW's integer ``steps_to_align`` (correct 1/N) became
    ``gain``, and the two overlapping tolerances -- ``stable_at_pixel`` (px) and
    the dead-band ``ignore_distance_um`` (um), of which the larger silently won
    -- became ONE radius in um. An old INI's keys are ignored on load.
    """

    images_to_average: int = 3         # frames per measurement (fewer = faster, noisier)
    gain: float = 0.5                  # fraction of the error corrected per move (1 = all)
    stable_radius_um: float = 0.2      # within this of the point: Stable, no correction
    settle_s: float = 0.2              # wait after a move before measuring again
    move_with_x: bool = True
    move_with_y: bool = True


@dataclass
class Limits:
    """The SAFETY ENVELOPE.  The brain clamps every stage/Z target to this."""

    motor_x_min: float = 0.0           # stage travel, um
    motor_x_max: float = 130.0
    motor_y_min: float = 0.0
    motor_y_max: float = 130.0
    z_min_v: float = 0.0               # Z piezo drive range, volts
    z_max_v: float = 75.0
    enforce: bool = True               # master switch for clamping


@dataclass
class Hardware:
    """How to reach the motion hardware.

    ``motion`` picks the rig:
      * "kim"   -- XY AND Z all go through the kim-control SERVICE (KIM101 +
                   3x PIA25: X = ch1, Y = ch2, Z = ch3, mapped inside kim).
                   See backends/remote_kim.py. This is the lab rig (2026-09-13).
      * "piezo" -- XY through piezo-control (backends/remote_xy.py), Z through
                   zpiezo-control or an own KCube (the use_*_z flags below).
    """

    motion: str = "kim"                # "kim" | "piezo"
    # Show and jog XY in "steps" or "um". Steps are what an open-loop stage
    # really counts; its um are steps x kim's um_per_step. A stage without a
    # step counter (piezo) is always um.
    xy_unit: str = "steps"
    kim_host: str = "127.0.0.1"        # kim-control host
    kim_cmd_port: int = 5567           # kim-control command (REP) port
    kim_pub_port: int = 5568           # kim-control status (PUB) port
    use_remote_xy: bool = True         # piezo rig: True = piezo service; False = sim
    piezo_host: str = "127.0.0.1"
    piezo_cmd_port: int = 5561         # piezo-control command (REP) port
    piezo_pub_port: int = 5562         # piezo-control status (PUB) port
    use_z: bool = True                 # False: ignore Z (no autofocus hardware)
    use_remote_z: bool = True          # True: command the zpiezo SERVICE; False: own KCube
    z_host: str = "127.0.0.1"          # zpiezo-control host
    z_cmd_port: int = 5565             # zpiezo-control command (REP) port
    z_pub_port: int = 5566             # zpiezo-control status (PUB) port
    kcube_serial: str = ""             # Thorlabs KCube serial (own-KCube path only)
    z_step_v: float = 0.25             # GUI Z jog step, volts
    z_step_time_ms: float = 25.0       # settle time per Z step
    cam_device: str = "0"              # GenICam index / id for the real camera


@dataclass
class UI:
    """GUI appearance (applied at startup)."""

    theme: str = "dark"                # "dark" | "light" (see apps/theme.py)


@dataclass
class Config:
    """Top-level config: one of each group.

    dataclasses cannot have mutable defaults, so the groups are created in
    ``__post_init__`` rather than as field defaults.
    """

    camera: Camera = None
    image: Image = None
    spot: Spot = None
    pattern: Pattern = None
    autofocus: Autofocus = None
    scanning: Scanning = None
    stabilizer: Stabilizer = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        self.camera = self.camera or Camera()
        self.image = self.image or Image()
        self.spot = self.spot or Spot()
        self.pattern = self.pattern or Pattern()
        self.autofocus = self.autofocus or Autofocus()
        self.scanning = self.scanning or Scanning()
        self.stabilizer = self.stabilizer or Stabilizer()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()


# --------------------------------------------------------------------------- #
# INI persistence
# --------------------------------------------------------------------------- #
def _sections(cfg: Config) -> dict[str, object]:
    """Map INI section name -> the dataclass instance that fills it."""
    return {
        "Camera": cfg.camera,
        "Image": cfg.image,
        "Spot": cfg.spot,
        "Pattern": cfg.pattern,
        "Autofocus": cfg.autofocus,
        "Scanning": cfg.scanning,
        "Stabilizer": cfg.stabilizer,
        "Limits": cfg.limits,
        "Hardware": cfg.hardware,
        "UI": cfg.ui,
    }


def _cast(raw: str, type_name: str):
    """Turn an INI string back into the right Python type.

    Because of ``from __future__ import annotations`` the field type arrives as a
    NAME ("bool", "int", "float", "str"), not the type object.

    The bool case is the classic trap: ``bool("False")`` is True (any non-empty
    string is truthy), so we parse the text explicitly.
    """
    if type_name == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(float(raw))   # tolerate "3.0" in an int field
    if type_name == "float":
        return float(raw)
    return raw


def save_config(cfg: Config, path: str) -> None:
    """Write the config to ``path`` as INI (one section per group)."""
    # interpolation=None: a value is stored exactly as typed. The default
    # parser treats "%" as a template marker, so a save folder or camera name
    # containing "%" made Save fail ("invalid interpolation syntax") and a
    # hand-edited file with one failed to load (deep cleaning 2026-09-28).
    cp = configparser.ConfigParser(interpolation=None)
    for section, obj in _sections(cfg).items():
        cp[section] = {key: str(val) for key, val in asdict(obj).items()}
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


def load_config(path: str) -> Config:
    """Read an INI written by :func:`save_config` back into a Config.

    Unknown/missing keys are ignored so an older file still loads after a new
    field is added (that field just keeps its default).
    """
    cp = configparser.ConfigParser(interpolation=None)     # see save_config
    cp.read(path, encoding="utf-8")
    cfg = Config()
    for section, obj in _sections(cfg).items():
        if section not in cp:
            continue
        for fld in fields(obj):
            if fld.name in cp[section]:
                setattr(obj, fld.name, _cast(cp[section][fld.name], fld.type))
    # Keep free-text enums valid.
    if cfg.autofocus.mechanism not in FOCUS_MECHANISMS:
        cfg.autofocus.mechanism = "spot_area"
    if cfg.autofocus.routine not in AF_ROUTINES:
        cfg.autofocus.routine = "sweep"
    if cfg.autofocus.approach_from not in AF_SIDES:
        cfg.autofocus.approach_from = "below"
    if cfg.image.symmetry not in SYMMETRIES:
        cfg.image.symmetry = "none"
    if (cfg.ui.theme or "").lower() not in THEMES:
        cfg.ui.theme = "dark"
    if cfg.hardware.motion not in MOTIONS:
        cfg.hardware.motion = "kim"
    if cfg.spot.search_shape not in SEARCH_SHAPES:
        cfg.spot.search_shape = "rect"
    if cfg.spot.size_method not in SIZE_METHODS:
        cfg.spot.size_method = "threshold"
    if cfg.spot.clip_mode not in CLIP_MODES:
        cfg.spot.clip_mode = "local"
    if cfg.camera.sim_spot_model not in SIM_SPOTS:
        cfg.camera.sim_spot_model = "gaussian"
    if cfg.camera.sim_bit_depth not in SIM_BIT_DEPTHS:
        cfg.camera.sim_bit_depth = 8
    if cfg.hardware.xy_unit not in XY_UNITS:
        cfg.hardware.xy_unit = "steps"
    return cfg
