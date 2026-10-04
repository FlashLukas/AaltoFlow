"""describe.py -- the spectrometer's self-description: what can be shown,
driven and scanned.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. Nothing here restates a value
that lives somewhere else; every bound is read from the Spectrometer when the
manifest is built.

The main detector is an ARRAY: `spectrum`, one REAL intensity per pixel, with
its own hardware-"swept" dimension `wavelength` (the grating spreads the light
over the CCD, so all 3648 wavelengths are measured at once):

  * `dims` names that dimension; its coordinate (nm, the instrument's own
    calibration) comes from `get_wavelengths`, read ONCE per scan.
  * `read` says how to fetch the value: a COMMAND (`get_trace`), because a
    spectrum is too big for the status stream. It arrives as a plain list.
  * `acquire` makes the scan trigger fresh scans and wait for THAT acquisition
    (acq_id, gotcha #17) before reading -- a cold read would return the
    previous point's spectrum and nothing would raise.

Scalar detectors in the same acquire group (peak wavelength, peak intensity,
integrated intensity, saturated) cost no extra exposure: one trigger, one wait,
several reads.

ACTIONS WITH A `wait` BLOCK. `take_dark` is safe to run from a scan routine
("close the shutter, take the dark, open it, scan"), and the `wait` block is
how the module says so AND how a caller knows it finished: take the id from the
reply, then wait until status shows that id and not acquiring. `clear_dark` is
immediate. `acquire` / `abort` carry no `wait`: they are front-panel buttons,
and a scan acquires through the detectors' `acquire` block.

What is dynamic: the analysis window's ends bound each other, and the
acquisition timeout grows with integration time x averages (a 60 s exposure
averaged 10 times must not time out at 30 s). Any change of those changes
`revision`, which every status frame carries as `describe_rev`, so clients
re-fetch. The simulated-light group exists only when the backend IS the
simulator: on the real instrument those knobs would change nothing.
"""

from __future__ import annotations

import json
import math
import zlib

from ..spectrometer import SIM_LIMITS

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, acquire=None, help="", **extra):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract.
    `extra` carries the array-detector fields (dtype, shape, dims, read) and an
    action's `wait` block."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("scale", scale),
                 ("set", set), ("settle", settle), ("args", args),
                 ("acquire", acquire), ("help", help), *extra.items()):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def manifest_revision(manifest: dict) -> int:
    """CRC over the manifest with `value` stripped -- derived, never hand-bumped."""
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` (a list of keys/indices) in a status dict."""
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


def acquisition_timeout_s(cfg) -> float:
    """How long a caller may wait for one acquisition: the configured minimum,
    or 1.5 x the time the scans themselves take plus 10 s, whichever is longer."""
    sc = cfg.scan
    need = 1.5 * max(1, int(sc.averages)) * (float(sc.integration_time_s) + 0.004) + 10.0
    return round(max(float(cfg.acquisition.timeout_s), need), 1)


def build_manifest(ccs200) -> dict:
    cfg = ccs200.cfg
    lim = cfg.limits
    wlo_lo, wlo_hi = ccs200.window_min_limits()
    whi_lo, whi_hi = ccs200.window_max_limits()
    simulated = bool(getattr(ccs200, "simulated", True))
    timeout = acquisition_timeout_s(cfg)
    pixels = int(ccs200.wavelengths().size) or 3648

    acquire = {
        "group": "spectrum",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": timeout,
    }
    wl_dim = [{"name": "wavelength", "label": "Wavelength", "unit": "nm",
               "length": pixels, "coord_verb": "get_wavelengths", "coord_key": "values"}]

    def sim_ctrl(name, label, unit, order, decimals, help):
        lo, hi = SIM_LIMITS[name]
        return _p(name, label, "control", "float", unit=unit, group="Light (simulation)",
                  order=order, min=lo, max=hi, decimals=decimals, read_path=[name],
                  set={"verb": "set_sim", "arg": "value", "extra": {"name": name}},
                  settle={"policy": "echoes", "key": name, "tol": 1e-9}, help=help)

    params = [
        # -- scan settings --------------------------------------------------------------
        _p("integration_time", "Integration time", "control", "float", unit="ms",
           group="Scan", order=10, scale=1e-3, decimals=3, step=1.0,
           min=lim.integration_min_s / 1e-3, max=lim.integration_max_s / 1e-3,
           read_path=["integration_time_s"],
           set={"verb": "set_integration_time", "arg": "integration_time_s"},
           settle={"policy": "echoes", "key": "integration_time_s", "tol": 1e-12},
           help="Exposure of one scan. Signal AND dark current grow with it; "
                "a dark only fits the time it was taken at."),
        _p("averages", "Averages", "control", "int", group="Scan", order=20, step=1,
           min=lim.averages_min, max=lim.averages_max, read_path=["averages"],
           set={"verb": "set_averages", "arg": "averages"},
           settle={"policy": "echoes", "key": "averages", "tol": 0.5},
           help="Scans averaged per acquisition (noise falls as 1/sqrt(N))."),
        _p("dark_subtract", "Subtract dark", "control", "bool", group="Scan", order=30,
           read_path=["dark_subtract"], set={"verb": "set_dark_subtract", "arg": "on"},
           settle={"policy": "echoes", "key": "dark_subtract"},
           help="Subtract the latched dark spectrum. With it on, an acquisition is "
                "REFUSED unless a dark at the current integration time exists."),
        _p("continuous", "Continuous scanning", "control", "bool", group="Scan", order=40,
           read_path=["continuous"], set={"verb": "set_continuous", "arg": "on"},
           settle={"policy": "echoes", "key": "continuous"},
           help="Scan on its own between acquisitions, like a live view."),
        _p("scan_time", "Scan time", "indicator", "float", unit="s", group="Scan",
           order=50, decimals=4, read_path=["scan_time_s"]),

        # -- analysis window ---------------------------------------------------------------
        _p("window_min", "Window start", "control", "float", unit="nm", group="Analysis",
           order=10, decimals=2, step=1.0, min=wlo_lo, max=wlo_hi,
           read_path=["window_min_nm"], set={"verb": "set_window_min", "arg": "nm"},
           settle={"policy": "echoes", "key": "window_min_nm", "tol": 1e-6},
           help="Peak and integrated intensity look only inside the window."),
        _p("window_max", "Window end", "control", "float", unit="nm", group="Analysis",
           order=20, decimals=2, step=1.0, min=whi_lo, max=whi_hi,
           read_path=["window_max_nm"], set={"verb": "set_window_max", "arg": "nm"},
           settle={"policy": "echoes", "key": "window_max_nm", "tol": 1e-6}),

        # -- measurement: the scan detectors (one acquisition feeds all of them) ------
        _p("spectrum", "Spectrum", "indicator", "array", unit="full scale",
           group="Measurement", order=10, acquire=acquire, dtype="float",
           shape=["wavelength"], dims=wl_dim,
           read={"verb": "get_trace", "key": "spectrum", "args": {"which": "sample"}},
           help="Intensity per pixel as a fraction of full scale (1.0 = saturated), "
                "averaged over the acquisition, dark-subtracted when that is on."),
        _p("peak_wavelength", "Peak wavelength", "indicator", "float", unit="nm",
           group="Measurement", order=20, decimals=3,
           read_path=["sample", "peak_nm"], acquire=acquire,
           help="Highest point inside the analysis window, refined to a fraction "
                "of a pixel with a parabola through three points."),
        _p("peak_intensity", "Peak intensity", "indicator", "float", unit="full scale",
           group="Measurement", order=21, decimals=4,
           read_path=["sample", "peak_intensity"], acquire=acquire),
        _p("integrated_intensity", "Integrated intensity", "indicator", "float",
           unit="full scale nm", group="Measurement", order=22, decimals=4,
           read_path=["sample", "integrated"], acquire=acquire,
           help="Area under the spectrum inside the analysis window (trapezoid in nm)."),
        _p("saturated", "Saturated", "indicator", "bool", group="Measurement", order=23,
           read_path=["sample", "saturated"], acquire=acquire,
           help="A pixel reached full scale in one of the averaged scans: the "
                "intensities are clipped and not to be trusted."),
        _p("acquire", "Acquire spectrum", "action", "action", group="Measurement", order=1,
           help="Average the next fresh scans and latch the result."),
        # a SAFETY verb (control.py): a viewer may always cancel an acquisition
        _p("abort", "Abort acquisition", "action", "action", group="Measurement", order=2,
           help="Cancel a running acquisition or dark. Allowed for anyone, also "
                "a viewer."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=3, read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=4, read_path=["acq_id"]),

        # -- dark -----------------------------------------------------------------------------
        _p("take_dark", "Take dark", "action", "action", group="Dark", order=1,
           wait={"target_key": "acq_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                           "flag_key": "acquiring", "invert": True},
                 "timeout_s": timeout},
           help="Acquire like `acquire` and keep the mean as the dark spectrum. "
                "BLOCK THE LIGHT FIRST: the module cannot tell a dark from a spectrum."),
        _p("clear_dark", "Clear dark", "action", "action", group="Dark", order=2,
           wait={"ready": {"policy": "immediate"}}),
        _p("dark_present", "Dark present", "indicator", "bool", group="Dark", order=10,
           read_path=["dark", "present"]),
        _p("dark_matches", "Dark fits integration time", "indicator", "bool", group="Dark",
           order=11, read_path=["dark", "matches"]),
        _p("dark_integration_time", "Dark integration time", "indicator", "float",
           unit="ms", scale=1e-3, group="Dark", order=12, decimals=3,
           read_path=["dark", "integration_time_s"]),

        # -- live -------------------------------------------------------------------------------
        _p("live_peak_wavelength", "Peak wavelength (live)", "indicator", "float",
           unit="nm", group="Live", order=10, decimals=3, plottable=True,
           read_path=["peak_nm"]),
        _p("live_peak_intensity", "Peak intensity (live)", "indicator", "float",
           unit="full scale", group="Live", order=11, decimals=4, plottable=True,
           read_path=["peak_intensity"]),
        _p("live_integrated", "Integrated intensity (live)", "indicator", "float",
           unit="full scale nm", group="Live", order=12, decimals=4, plottable=True,
           read_path=["integrated"]),
        _p("exposure", "Exposure (highest raw pixel)", "indicator", "float",
           unit="full scale", group="Live", order=13, decimals=3, plottable=True,
           read_path=["exposure"],
           help="Aim for 0.5 - 0.9: brighter saturates, dimmer wastes dynamic range."),
        _p("live_saturated", "Saturated (live)", "indicator", "bool", group="Live",
           order=14, read_path=["saturated"]),
        _p("scans", "Scans", "indicator", "int", group="Live", order=15,
           read_path=["scans"]),

        # -- status ----------------------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]

    if simulated:
        params += [
            _p("light_on", "Light on input", "control", "bool", group="Light (simulation)",
               order=1, read_path=["light_on"], set={"verb": "set_light", "arg": "on"},
               settle={"policy": "echoes", "key": "light_on"},
               help="Off = the fibre is capped: what take_dark needs."),
            sim_ctrl("lamp_level_per_s", "Lamp level", "FS/s", 10, 2,
                     "The lamp continuum at its maximum, full scale per second."),
            sim_ctrl("lamp_temperature_K", "Lamp temperature", "K", 20, 0,
                     "Colour temperature of the lamp (tungsten ~2800-3200 K)."),
            sim_ctrl("line_level_per_s", "Line level", "FS/s", 30, 2,
                     "The brightest emission line (Hg 546.07 nm) at its peak."),
            sim_ctrl("line_fwhm_nm", "Resolution (FWHM)", "nm", 40, 2,
                     "Instrument line width; the CCS200 is < 2 nm at 633 nm."),
            sim_ctrl("dark_rate_per_s", "Dark current", "FS/s", 50, 4,
                     "Grows linearly with integration time."),
            sim_ctrl("offset", "Offset", "full scale", 60, 4, "Electronic offset."),
            sim_ctrl("read_noise", "Read noise", "full scale", 70, 4, "rms per pixel per scan."),
        ]

    # Bounds that are not known must not appear as NaN.
    for d in params:
        for k in ("min", "max"):
            if k in d and not (isinstance(d[k], (int, float)) and math.isfinite(d[k])):
                del d[k]

    label = ("CCD spectrometer CCS200 (simulated)" if simulated
             else "Thorlabs CCS200/M spectrometer")
    manifest = {"schema": SCHEMA_VERSION, "module": "ccs200", "label": label,
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
