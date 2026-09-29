"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.

Two consumers, two projections of one source: the reconfigurable control screen
reads this manifest DIRECTLY (it wants buttons, their arguments, their danger
flags and the group/order layout hints), while scan-core projects it into
Settables and Gettables. Full contract: INSTRUMENT_MODULE_GUIDE.md section 6b.

This module is where the idea started. `backends/base.py` already defines a
GenICam-style `features()` returning descriptors so the GUI can build controls
for ANY camera generically; `describe` is that pattern raised to the level of
the whole suite. The two are deliberately kept separate: `features()` is the
CAMERA HARDWARE's parameter set, which varies per device and is enumerated from
the driver at runtime, while this manifest is the MODULE's own surface -- the
vision loops, the focus, the tracked spot. A control screen can show both.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from cfg when the manifest is built.

XY IS NOT A PER-AXIS CONTROL HERE, on purpose. The service has only `move_xy`,
which takes both axes at once, so there is no per-axis verb to point a Settable
at -- and inventing one would give the rig two competing paths to the same
motors, since this module owns no motion hardware and drives XY through
piezo-control. So XY appears as indicators plus a `move_xy` ACTION with two
arguments, and a panel that wants to drive XY should drive the piezo module.
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, help="", wait=None, stream=None):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,      # keys/indices into the status dict, or None
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("scale", scale),
                 ("set", set), ("settle", settle), ("args", args),
                 ("help", help), ("wait", wait), ("stream", stream)):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def manifest_revision(manifest: dict) -> int:
    """A checksum over the parts of the manifest a client must react to.

    Derived, not hand-bumped -- a counter someone has to remember to increment
    is a counter that will eventually be wrong. `value` is excluded on purpose:
    it changes many times a second and travels in the status stream anyway, so
    including it would tell clients to re-fetch constantly and mean nothing.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` against a status dict.

    A list of keys rather than a dotted string, because channel names and ids
    can contain dots and a dotted path could not be split back apart. An int
    element indexes into a per-axis list.
    """
    if not path:
        return None
    cur = status
    for key in path:
        if isinstance(key, int) and isinstance(cur, (list, tuple)):
            if not -len(cur) <= key < len(cur):
                return None
            cur = cur[key]
            continue
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def build_manifest(brain) -> dict:
    """The vision brain's own surface: focus, the two loops, and what it sees."""
    try:
        objectives = list(brain.list_objectives() or [])
    except Exception:
        objectives = []

    # Unit and bounds come from the LIVE Z / XY backends: volts on the piezo
    # rig, micrometres on the KIM rig, whose range follows kim's leash. When a
    # bound changes, so does the revision, and clients re-fetch.
    # the laser coordinates can reach +- the image size (the template must stay in view)
    frame = getattr(brain, "_last_frame", None)
    h_px, w_px = (frame.shape[:2] if frame is not None else (480, 640))
    lx = round(float(w_px) * float(brain.cfg.image.pixel_size_x_um), 3)
    ly = round(float(h_px) * float(brain.cfg.image.pixel_size_y_um), 3)
    z_unit = brain.z_unit()
    zlim = brain.z_limits() or (None, None)
    # Settle tolerance of "z". On the KIM rig Z moves in whole STEPS: kim rounds
    # a um target to steps and reads back steps x um_per_step, up to HALF a step
    # away from what was asked. With the fixed 0.01 um tolerance and kim's
    # 0.02 um step, a scan level such as 0.25 um (12.5 steps -> 12 -> 0.24)
    # never "echoed" and every such point waited out its timeout (deep
    # cleaning 2026-09-28). So: at least 0.6 step, never below the old 0.01.
    z_res = brain.z_resolution() if callable(getattr(brain, "z_resolution", None)) else None
    z_tol = round(max(1e-2, 0.6 * z_res), 6) if z_res else 1e-2
    xylim = brain.xy_limits() or ((None, None), (None, None))
    if z_unit == "V":
        z_help = ("Drive voltage of the Z piezo, sent to zpiezo-control (or an "
                  "own KCube). The verb's argument is called 'volts'.")
    else:
        z_help = (f"Z position in {z_unit}, driven through kim-control (open-loop "
                  "inertia actuator: commanded, not measured). The verb's "
                  "argument is still called 'volts' for wire compatibility.")

    params = [
        # ---- focus ---------------------------------------------------------
        _p("z", "Focus (Z)", "control", "float", unit=z_unit, group="Focus",
           order=10, min=zlim[0], max=zlim[1], step=0.25, decimals=3,
           plottable=True, read_path=["z_voltage"],
           set={"verb": "set_z", "arg": "volts"},
           settle={"policy": "echoes", "key": "z_voltage", "tol": z_tol},
           help=z_help),

        _p("continuous_focus", "Continuous focus", "control", "bool",
           group="Focus", order=20, read_path=["continuous_focus_on"],
           set={"verb": "set_continuous_focus", "arg": "on"},
           settle={"policy": "echoes", "key": "continuous_focus_on"},
           help="Dither-climb focus that keeps running."),

        # ---- the two closed loops -----------------------------------------
        _p("tracking", "Pattern tracking", "control", "bool", group="Loops",
           order=30, read_path=["tracking_on"],
           set={"verb": "set_tracking", "arg": "on"},
           settle={"policy": "echoes", "key": "tracking_on"},
           help="Locates the template every frame and pins the scan grid to it."),

        _p("stabilize", "Stabiliser", "control", "bool", group="Loops",
           order=40, read_path=["stabilize_on"],
           set={"verb": "set_stabilize", "arg": "on"},
           settle={"policy": "echoes", "key": "stabilize_on"},
           help="Nulls the spot-to-selected-point distance by nudging XY. "
                "Averages N frames and corrects ONCE on the mean; correcting "
                "every frame caused a limit cycle."),

        _p("objective", "Objective", "control", "enum", group="Optics",
           order=50, options=objectives or None,
           read_path=["objective_name"],
           set={"verb": "set_objective", "arg": "name"},
           settle={"policy": "echoes", "key": "objective_name"},
           help="Changes the pixel size, and so every micrometre conversion."),

        # ---- what it sees --------------------------------------------------
        _p("spot_found", "Spot found", "indicator", "bool", group="Spot",
           order=60, read_path=["spot_found"]),
        _p("spot_x", "Spot X", "indicator", "float", unit="px", group="Spot",
           order=61, decimals=2, plottable=True, read_path=["spot_x"]),
        _p("spot_y", "Spot Y", "indicator", "float", unit="px", group="Spot",
           order=62, decimals=2, plottable=True, read_path=["spot_y"]),
        _p("spot_area", "Spot area", "indicator", "float", unit="px2",
           group="Spot", order=63, decimals=1, plottable=True,
           read_path=["spot_area"],
           help="Thresholded area (fixed threshold). Also usable as an autofocus metric."),
        # The spot's size WITHOUT a threshold (2026-09-28): a defocused
        # coherent spot has rings and a hole that a fixed threshold cuts
        # wrongly. sigma^2 is exactly a parabola in Z for a coherent beam.
        _p("spot_d4sigma", "Spot D4sigma", "indicator", "float", unit="px",
           group="Spot", order=64, decimals=2, plottable=True,
           read_path=["spot_d4sigma_px"],
           help="ISO 11146 second-moment diameter 4 sqrt(sigma^2), no threshold. "
                "NaN when no spot is measurable."),
        _p("spot_sigma2", "Spot sigma^2", "indicator", "float", unit="px2",
           group="Spot", order=65, decimals=2, plottable=True,
           read_path=["spot_sigma2_px2"],
           help="Second moment of the intensity about its centroid (mean of x and "
                "y). The spot_d4sigma autofocus metric; a parabola in Z."),
        _p("spot_rel_area", "Spot area (relative level)", "indicator", "float",
           unit="px2", group="Spot", order=66, decimals=1, plottable=True,
           read_path=["spot_rel_area"],
           help="Area above rel_level (default 1/e^2) of the spot's own peak. "
                "The spot_relative autofocus metric."),
        _p("spot_saturated", "Spot saturated", "indicator", "bool", group="Spot",
           order=67, read_path=["spot_saturated"],
           help="A pixel of the spot at the camera's maximum: the sizes are then "
                "wrong (sigma^2 too big). Lower the exposure."),

        _p("match_found", "Template matched", "indicator", "bool",
           group="Pattern", order=70, read_path=["match_found"]),
        _p("match_score", "Match score", "indicator", "float", group="Pattern",
           order=71, decimals=4, plottable=True, read_path=["match_score"]),
        # A LOST pattern is a latched fault (2026-09-28): scan-core pauses on a
        # non-empty `fault`, and it stays until a human clears it.
        _p("fault", "Fault", "indicator", "string", group="Status", order=3,
           read_path=["fault"],
           help="Empty when fine. Set (and LATCHED) when the tracked pattern is "
                "lost -- the text names the likely cause: out of image, the laser "
                "spot on the pattern, or out of focus. The stabiliser holds the "
                "stage and no point counts as settled until Clear fault."),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=4, read_path=["hw_error"],
           help="Empty when fine; a failed camera grab or stage / Z read-back."),
        _p("clear_fault", "Clear fault", "action", "action", group="Pattern",
           order=72,
           help="After a lost pattern: correct the cause first (focus, move the "
                "pattern back into view, move the spot off it -- or switch "
                "tracking off and on to search the whole frame, watching the "
                "result), then clear. Refused while tracking is on and the "
                "pattern is still not found."),

        _p("distance_um", "Spot to point", "indicator", "float", unit="um",
           group="Loops", order=80, decimals=3, plottable=True,
           read_path=["distance_um"],
           help="What the stabiliser is nulling."),
        _p("stable", "Stable", "indicator", "bool", group="Loops", order=81,
           read_path=["stable"]),

        # ---- scanning the camera's array: a point per scan step --------------
        # Two independent axes so a scan can nest them. The bound is the array
        # size, looked up live (it changes when the array is redrawn). Settle:
        # the service must ADOPT the new index, and only then is `point_settled`
        # believed -- a stale True from the previous point would otherwise
        # record every point one step behind. It needs tracking + stabiliser on
        # and a calibrated spot, or the axis times out rather than lying.
        _p("scan_ix", "Scan point X (index)", "control", "int", group="Scan array",
           order=82, min=0, max=max(0, brain.cfg.scanning.points_x - 1), step=1,
           read_path=["selected_index_x"],
           set={"verb": "set_selected_index", "arg": "ix"},
           settle={"policy": "adopt_then_flag", "setpoint_key": "selected_index_x",
                   "flag_key": "point_settled"},
           help="Selects a column of the scan array; the stabiliser brings that point "
                "under the laser. Needs pattern tracking and the stabiliser ON."),
        _p("scan_iy", "Scan point Y (index)", "control", "int", group="Scan array",
           order=83, min=0, max=max(0, brain.cfg.scanning.points_y - 1), step=1,
           read_path=["selected_index_y"],
           set={"verb": "set_selected_index", "arg": "iy"},
           settle={"policy": "adopt_then_flag", "setpoint_key": "selected_index_y",
                   "flag_key": "point_settled"},
           help="Selects a row of the scan array; see Scan point X."),
        _p("point_x_px", "Scan point X", "indicator", "float", unit="px",
           group="Scan array", order=84, decimals=2, plottable=True,
           read_path=["selected_point_x"],
           help="Where the selected array point is in the image."),
        _p("point_y_px", "Scan point Y", "indicator", "float", unit="px",
           group="Scan array", order=85, decimals=2, plottable=True,
           read_path=["selected_point_y"]),
        _p("point_settled", "Point settled", "indicator", "bool", group="Scan array",
           order=86, read_path=["point_settled"],
           help="A full averaged stabiliser window at THIS point was within the "
                "stable radius."),
        _p("spot_from_template_x", "Spot from template X", "indicator", "float",
           unit="um", group="Scan array", order=87, decimals=3, plottable=True,
           read_path=["spot_from_template_x_um"],
           help="Laser spot position on the sample measured from the MAIN (first) "
                "template, image +x right. Valid while a backup pattern drives."),
        _p("spot_from_template_y", "Spot from template Y", "indicator", "float",
           unit="um", group="Scan array", order=88, decimals=3, plottable=True,
           read_path=["spot_from_template_y_um"],
           help="As X; image +y is DOWN."),

        # ---- the laser ON THE SAMPLE, in template coordinates ---------------
        # A point of the sample, measured optically from the main template --
        # immune to an open-loop stage's counter drift. Setting one PLACES the
        # laser there (closed loop on the camera image, like the stabiliser),
        # and a fly scan can bin by it: fly the stage, record laser_x. Bounds =
        # the image size either way: the template has to stay in view (or a
        # backup pattern take over). Settle: the target echoed AND
        # laser_settled, which is re-evaluated every frame.
        _p("laser_x", "Laser on sample X", "control", "float", unit="um",
           group="Laser on sample", order=89, decimals=3, plottable=True,
           min=-lx, max=lx, read_path=["spot_from_template_x_um"],
           set={"verb": "set_laser_target", "arg": "x"},
           settle={"policy": "adopt_then_flag", "setpoint_key": "laser_target_x_um",
                   "flag_key": "laser_settled"},
           stream={"group": "laser", "channel": "laser_x"},
           help="Where the laser is on the sample, um from the main template "
                "(image +x right). Setting it moves the sample until the laser is "
                "there. Needs tracking and a calibrated spot. In a FLY scan: the "
                "coordinate the image is binned by."),
        _p("laser_y", "Laser on sample Y", "control", "float", unit="um",
           group="Laser on sample", order=90, decimals=3, plottable=True,
           min=-ly, max=ly, read_path=["spot_from_template_y_um"],
           set={"verb": "set_laser_target", "arg": "y"},
           settle={"policy": "adopt_then_flag", "setpoint_key": "laser_target_y_um",
                   "flag_key": "laser_settled"},
           stream={"group": "laser", "channel": "laser_y"},
           help="As X; image +y is DOWN. As the outer axis of a fly scan it puts "
                "each row on the sample, whatever the stage counter says."),
        _p("laser_settled", "Laser placed", "indicator", "bool",
           group="Laser on sample", order=91, read_path=["laser_settled"]),

        # ---- position (read-only here; drive XY through piezo-control) -----
        _p("stage_x", "Stage X", "indicator", "float", unit="um",
           group="Position", order=90, decimals=3, plottable=True,
           read_path=["stage_x"]),
        _p("stage_y", "Stage Y", "indicator", "float", unit="um",
           group="Position", order=91, decimals=3, plottable=True,
           read_path=["stage_y"]),
        _p("stage_moving", "Stage moving", "indicator", "bool",
           group="Position", order=92, read_path=["stage_moving"]),
        _p("stage_ok", "Stage answering", "indicator", "bool",
           group="Position", order=93, read_path=["stage_ok"],
           help="False while the stage service (kim) does not answer: stage "
                "controls are refused at once instead of waiting on a timeout."),
        _p("reconnect_stage", "Reconnect stage", "action", "action",
           group="Position", order=94,
           help="Rebuild the connection to the stage service, e.g. after it was restarted."),

        # ---- housekeeping --------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("fps", "Frame rate", "indicator", "float", unit="1/s",
           group="Status", order=2, decimals=1, plottable=True,
           read_path=["fps"]),
        _p("af_running", "Autofocus running", "indicator", "bool",
           group="Focus", order=21, read_path=["af_running"]),
        _p("af_error", "Autofocus state", "indicator", "string", group="Focus",
           order=22, read_path=["af_error"]),
        _p("pixel_size_x", "Pixel size", "indicator", "float", unit="um",
           group="Optics", order=51, decimals=4, read_path=["pixel_size_x"]),

        # ---- actions -------------------------------------------------------
        # A `wait` block makes this a scan ROUTINE action (scan-core
        # `camera.autofocus`): send `autofocus`, take the af_id it replies with,
        # wait until status shows THAT id finished, then require af_error "OK"
        # -- a failed or killed run raises, and the routine's on_error decides
        # whether the scan stops. Waiting on the id, not just on af_running,
        # is gotcha #17: a frame from before the request also says "not
        # running". Tracking + stabiliser pause during the run (see
        # Camera._do_autofocus).
        _p("autofocus", "Find focus", "action", "action", group="Focus",
           order=100,
           wait={"target_key": "af_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "af_id",
                           "flag_key": "af_running", "invert": True},
                 "check": {"key": "af_error", "equals": "OK"},
                 "timeout_s": float(brain.cfg.autofocus.scan_timeout_s)},
           help="Finds focus (routine: sweep or one_way, AutoFocus tab) and parks "
                "there. Tracking and the stabiliser pause while it runs."),
        _p("af_id", "Autofocus #", "indicator", "int", group="Focus", order=23,
           read_path=["af_id"]),
        _p("af_hint", "Autofocus warning", "indicator", "string", group="Focus", order=24,
           read_path=["af_hint"],
           help="Empty when fine. Set when spot_area is the metric and the spot is NOT "
                "saturated: its thresholded area is then largest at focus -- use "
                "spot_d4sigma."),
        # Z STEP CALIBRATION (2026-09-28): the camera measures how far the
        # open-loop Z really moves up vs down per counter step, and writes the
        # two step sizes into the Z stage (kim). Numbered and waitable like
        # the autofocus; a refused run (poor fit, minimum not bracketed)
        # raises in a scan routine through the check.
        _p("calibrate_z_steps", "Calibrate Z steps", "action", "action", group="Focus",
           order=102,
           wait={"target_key": "zcal_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "zcal_id",
                           "flag_key": "zcal_running", "invert": True},
                 "check": {"key": "zcal_state", "equals": "OK"},
                 "timeout_s": float(brain.cfg.autofocus.scan_timeout_s)},
           help="Needs a calibrated spot, roughly in focus, and the kim Z. Walks Z up "
                "and down through focus measuring sigma^2 (D4sigma); the ratio of the "
                "two parabolas' curvatures is (step up / step down)^2. Writes both "
                "step sizes to kim (geometric mean kept). Refuses rather than guess."),
        _p("zcal_ratio", "Z step ratio up/down", "indicator", "float", group="Focus",
           order=25, decimals=4, read_path=["zcal_ratio"],
           help="Result of the last Z step calibration (NaN before one / when refused)."),
        _p("zcal_state", "Z calibration state", "indicator", "string", group="Focus",
           order=26, read_path=["zcal_state"]),
        _p("spot_bit_depth", "Spot size bit depth", "indicator", "int", group="Spot",
           order=68, read_path=["spot_bit_depth"],
           help="8, or the camera's full depth (e.g. 12) when it delivers one: the "
                "spot sizes are then measured on that frame (far wings not rounded away)."),
        _p("spot_bit_note", "Why 8-bit spot sizes", "indicator", "string", group="Spot",
           order=69, read_path=["spot_bit_note"],
           help="Empty when fine. Otherwise why the spot sizes run on 8-bit frames, e.g. "
                "the camera's PixelFormat is Mono8 (set Mono10/12 in IDS peak Cockpit; "
                "this module never changes it)."),
        _p("kill_af", "Kill autofocus", "action", "action", group="Focus",
           order=101, danger=True),
        # 2026-09-29: where the size was measured, the new sizes, saturation as
        # information, and the autofocus exposure
        _p("spot_offset_px", "Spot offset from calibration", "indicator", "float",
           group="Spot", order=70, unit="px", decimals=1, read_path=["spot_offset_px"],
           help="How far the located spot (Spot.locate = peak / blob) is from the "
                "calibrated position. Large = the laser moved or the calibration is stale."),
        _p("spot_size_why", "Why no spot size", "indicator", "string", group="Spot",
           order=71, read_path=["spot_size_why"],
           help="Empty when measured; otherwise why, and where the brightest light is."),
        _p("spot_d86", "Spot D86 (encircled)", "indicator", "float", group="Spot",
           order=72, unit="px", decimals=2, read_path=["spot_d86_px"],
           help="Diameter holding encircled_fraction (0.86) of the spot's energy; no "
                "threshold, rings and a hole counted where they are."),
        _p("spot_gauss_sigma2", "Spot sigma^2 (Gaussian fit)", "indicator", "float",
           group="Spot", order=73, unit="px^2", decimals=2,
           read_path=["spot_gauss_sigma2_px2"],
           help="A 2-D Gaussian fit's sigma^2. Not usable while saturated (flat top)."),
        _p("spot_peak_avg", "Spot peak", "indicator", "float", group="Spot", order=74,
           unit="counts", decimals=1, read_path=["spot_peak_avg"],
           help="3x3-averaged peak above background. Not usable while saturated."),
        _p("spot_sat_fraction", "Spot saturated fraction", "indicator", "float",
           group="Spot", order=75, decimals=3, read_path=["spot_sat_fraction"]),
        _p("af_exposure_active", "Autofocus exposure on", "indicator", "bool",
           group="Focus", order=27, read_path=["af_exposure_active"],
           help="autofocus.exposure_us is on the camera now (an autofocus / Z "
                "calibration / spot calibration runs at it; restored after)."),
        _p("auto_exposure_once", "Auto exposure (once)", "action", "action",
           group="Camera", order=110,
           help="Sets ExposureTime once so the image (the spot's region left out) is "
                "well exposed: the camera's ExposureAuto=Once if it has it, else a few "
                "software steps. Changes only ExposureTime."),
        # Saved NEXT TO the measurement when run as a scan routine: scan-core
        # fills {data_dir} / {data_stem} / {moment}. The save is done when the
        # command replies, so the wait is `immediate` -- the scan does not
        # wait for anything else.
        _p("save_scan_pattern", "Save pattern with the scan", "action", "action",
           group="Imaging", order=108,
           args=[{"name": "folder", "label": "Folder", "type": "string",
                  "default": "{data_dir}"},
                 {"name": "name", "label": "File name", "type": "string",
                  "default": "{data_stem}_{moment}_pattern"}],
           wait={"ready": {"policy": "immediate"}, "timeout_s": 10.0},
           help="Save the pattern (with its scan array) and a .json record. In a "
                "scan routine it goes into the measurement's folder."),
        _p("save_picture", "Save camera picture", "action", "action",
           group="Imaging", order=109,
           args=[{"name": "folder", "label": "Folder", "type": "string",
                  "default": "{data_dir}"},
                 {"name": "name", "label": "File name", "type": "string",
                  "default": "{data_stem}_{moment}_camera"}],
           wait={"ready": {"policy": "immediate"}, "timeout_s": 10.0},
           help="Save the current camera frame and a .json record (spot, pattern, "
                "stage, focus). In a scan routine it goes into the measurement's folder."),
        _p("snapshot", "Snapshot", "action", "action", group="Imaging",
           order=110,
           args=[{"name": "path", "label": "File path", "type": "string",
                  "default": None}]),
        _p("move_xy", "Move XY", "action", "action", group="Position",
           order=120, danger=True,
           args=[{"name": "x", "label": "X", "type": "float", "unit": "um",
                  "min": xylim[0][0], "max": xylim[0][1]},
                 {"name": "y", "label": "Y", "type": "float", "unit": "um",
                  "min": xylim[1][0], "max": xylim[1][1]}],
           settle={"policy": "flag_only", "key": "stage_moving",
                   "invert": True},
           help="Takes both axes at once, which is why XY is not a per-axis "
                "control here. To sweep XY, drive the stage's own module "
                "(kim-control or piezo-control) instead."),
    ]

    manifest = {"schema": SCHEMA_VERSION, "module": "camera",
                "label": "Vision brain", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
