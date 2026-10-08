# Verified instruments

Which modules have run against the **real instrument**, what was checked, where the
tests are, and what the hardware taught us (the caveats). Everything else in the
suite runs in simulation only; its unverified hardware calls are marked `# VERIFY`
in the one backend file that talks to the vendor library.

*Verified* here means: the module's own service ran with `--real` on the lab PC,
talked to the device, and the result was checked by a person against the
instrument (a front panel, the vendor's own software, or a physical effect).
Tests in the repository are **offline** -- they replay what was learnt on the
hardware against fakes of the vendor library, so a regression is caught without
the instrument.

Keep this page up to date after every hardware pass: add the date, what was
checked, the commit(s) and the caveats. No serial numbers, PC or user names here
(those go into the private notes).

## Summary

| Module | Instrument | Status | Last hardware pass |
|---|---|---|---|
| [`pm16`](../modules/detector/pm16-control) | Thorlabs PM16-121 USB power meter | **verified**, incl. a scan-core scan | 2026-09-15 |
| [`kim`](../modules/motion/kim-control) | Thorlabs KIM101 + 3x PIA25 inertia stage | **verified** (moves, datum, camera calibration, rasters) | 2026-09-25 |
| [`camera`](../modules/imaging/camera-control) | IDS U3-38xCP (IDS peak) + KIM stage | **partly verified** (camera, features, spot, stabiliser, laser placement, save, spot-size metrics, one-way autofocus with D4sigma / D86 / Gauss, AF exposure, auto exposure, re-drawn template keeps the scan array) | 2026-10-08 |
| [`signalhound`](../modules/detector/signalhound-control) | Signal Hound SA44B + USB-TG44A | **verified** (spectrum mode, TG CW + TG sweep via shsg / shsna) | 2026-09-28 |
| [`shsg`](../modules/source/shsg-control) | the USB-TG44A as a CW source (client of signalhound) | **verified** (CW level/frequency, RF off = park, restore after a TG sweep) | 2026-09-28 |
| [`shsna`](../modules/detector/shsna-control) | scalar network analyser on the TG sweep (client of signalhound) | **verified** (reference + transmission, grid) | 2026-09-28 |
| [`dssg`](../modules/source/dssg-control) | DS Instruments SG12000L (fw V7.84) | **verified** (vernier measured, fine power, per-unit power calibration) | 2026-10-07 |
| [`afg`](../modules/source/afg-control) | Tektronix AFG1062 | **verified** (levels, phase, follow, clamps, keep-outputs restart) | 2026-10-07 |
| [`scope`](../modules/detector/scope-control) | RS PRO RSDS1102CML+ (Siglent SDS1000CML+) | **verified** (records, timebases incl. slow, averaging, XY, units) | 2026-10-07 |
| scan-core | -- | real scans with pm16, kim + pm16 raster, fly scans (kim / camera coordinates), XY mask | 2026-10-08 |
| all others | -- | simulation only | -- |

## pm16 -- Thorlabs PM16-121 power meter (first module on real hardware)

**Checked (2026-09-15):** brain start + adopt of the meter's stored settings,
live readings at ~17/s, `acquire` (5 fresh readings in 0.35 s, mean +- sd), the
real service in its own process, a client, a scan-core registry built from
`describe` alone, and a wavelength scan 500 / 520 / 540 / 600 nm with acquired
detectors (the reading follows the responsivity).

**Tests:** [`tests/test_tlpmx.py`](../modules/detector/pm16-control/tests/test_tlpmx.py)
(the ctypes layer against a fake DLL),
[`test_meter.py`](../modules/detector/pm16-control/tests/test_meter.py),
[`test_hwlock.py`](../modules/detector/pm16-control/tests/test_hwlock.py).

**Caveats found on the hardware:**
- Driven through `TLPMX_64.dll` (Thorlabs Optical Power Monitor) with ctypes;
  NI-VISA cannot see the PM16 when Thorlabs' own USB driver is installed.
- Averaging is fixed at 60 ms (set average count/time is refused); one reading is
  ~58 ms and always new. Ranges 0.174 mW / 17.4 mW / 1.74 W; a set snaps UP.
- The vendor's "is it available?" query lies inside a process that has a ZeroMQ
  context -- trying to open is the only honest test (gotcha #23).
- A hard kill during a USB read leaves the meter answering "I/O error" until it is
  unplugged -- always stop services with the `shutdown` verb (gotcha #25).
- The fly-scan stream ran on the meter on 2026-09-28 (every fly scan under
  scan-core below; ~17 readings/s, so 2 -- 8 samples per pixel at 1 -- 2 um/s).
- Not re-checked on the meter since: the hwlock claim (2026-09-27; the service
  on the new code started and read normally, but no second service was tried).

## kim -- Thorlabs KIM101 + 3x PIA25 piezo inertia stage

**Checked (2026-09-13 .. 09-25):** open + adopt (the controller's own voltage /
rate / acceleration are kept), moves on all three channels from the service and
the GUI, Datum, the camera-based step calibration (px/step per voltage and
direction, full procedure on the rig), camera-driven image moves, and long
reflectivity rasters with pm16 (21 x 25 points).

**Tests:** [`tests/test_kinesis_backend.py`](../modules/motion/kim-control/tests/test_kinesis_backend.py)
(the serial-link lock found on the hardware),
[`test_pxcal.py`](../modules/motion/kim-control/tests/test_pxcal.py) (the calibration
against a fake camera with a rotated, asymmetric, voltage-dependent stage),
[`test_adopt_on_start.py`](../modules/motion/kim-control/tests/test_adopt_on_start.py),
[`test_um_calibration.py`](../modules/motion/kim-control/tests/test_um_calibration.py),
[`test_echo_and_hw_error.py`](../modules/motion/kim-control/tests/test_echo_and_hw_error.py).

**Caveats found on the hardware:**
- One USB link, many threads: status polling and commands in parallel
  desynchronised the replies ("unexpected channel in the reply") until every
  device call went through one lock. Close the Kinesis app first -- one program
  per controller.
- The KIM101 drives only enabled channels, in pairs (1,2) / (3,4): a Z move stops
  a running X/Y move and vice versa.
- Step size is far from the datasheet's 20 nm, different per DIRECTION (up to
  37 %), depends on the drive voltage and saturates above ~105 V; 95 V was the
  most symmetric point. Use the camera calibration, and recalibrate after a cable,
  voltage or objective change.
- Open loop: the step COUNTER drifted ~26 um from the true position over a
  525-point raster. Absolute kim coordinates are not reproducible across a long
  scan; approach from one direction, or let the camera close the loop.
- Not yet checked on the rig: the target-echo settle of 2026-09-28 (the KIM101 must
  report "moving" on the first query after a move -- `# VERIFY` in the backend).
- The fly-scan stream (position readback) ran on 2026-09-28: flown rows of
  +-5 um at 2 um/s and 20 um at 1 um/s, speed restored after every row (see
  scan-core below).

## camera -- IDS U3-38xCP camera, vision brain, KIM stage as XY/Z

**Checked (2026-09-13 .. 09-25):** the IDS peak driver (open, 237 camera features
in the live panel, 1936 x 1096 Mono8 at 20 fps), Set Z through kim, the spot
calibration at 63x, template tracking, the "stage not answering" handling with
kim switched off, and `save_picture` / `save_scan_pattern` from a real scan's
before/after routines.

**Tests:** [`tests/test_gui_smoke.py`](../modules/imaging/camera-control/tests/test_gui_smoke.py)
(replays the real camera's feature list,
[`tests/data/`](../modules/imaging/camera-control/tests/data)),
[`test_spot_detection.py`](../modules/imaging/camera-control/tests/test_spot_detection.py)
(a lab-like frame), [`test_remote_kim.py`](../modules/imaging/camera-control/tests/test_remote_kim.py),
[`test_adopt_on_start.py`](../modules/imaging/camera-control/tests/test_adopt_on_start.py),
[`test_hwlock.py`](../modules/imaging/camera-control/tests/test_hwlock.py).

**Caveats found on the hardware:**
- Close IDS peak Cockpit before starting the service (it holds the camera).
- The camera's power-on default (15 ms exposure) saturates every pixel on this
  microscope; ~1 ms is right. The module adopts whatever is set -- store a good
  set as the camera's default in IDS peak Cockpit.
- 45 integer features exceed the 32-bit range -- the GUI once crashed building
  them (fixed; the real list is a test fixture).
- The first spot calibration locked onto a saturated illumination corner (fixed:
  size limits and edge rejection for spot candidates).
- The stage axes are rotated 90 degrees against the image on the rig; the kim
  px/step calibration absorbs it. kim's table must match the objective in use,
  otherwise kim refuses image moves.
- Checked on the rig at 63x (2026-09-25 .. 09-28): the array stabiliser (array
  points brought under the laser from the suite's Control tab, and a 3 x 22 scan
  over the array with pm16), and laser placement (`set_laser_target`: 2 um in
  1.16 s, 0.06 um off; row placement in the camera-coordinate fly scans, 0.01 --
  0.15 um from the target, short on the side it comes from).
- Spot size without a fixed threshold (c6f60be), 2026-09-28 at 63x on plain film,
  exposure lowered to 65 us so the spot does not saturate (peak ~200 of 255;
  at the lab's 1.4 ms, 748 pixels were at 255). Z sweeps through focus, 3 repeats:
  - D4sigma is continuous and monotonic on both sides of focus (29.5 px at
    focus, ~120 px 3 focal depths out), with no step where the dark centre of the
    coherent spot appears (~3 um below and ~3.4 um above focus). The fixed
    threshold (at half the peak) loses the spot 0.4 um above focus.
  - sigma^2 is near a parabola close to focus (R^2 0.98 -- 0.997 up to 3x its
    minimum) but not over the whole range (R^2 0.97 -- 0.99): the far wings read
    up to 50 % LOW -- the 8-bit flattening (far out the peak is 11 -- 16 grey
    levels). The spot is astigmatic: the x and y minima are ~0.7 um apart.
  - On the same recorded frames: detect_px 1/2/3 and smooth_px 0/2 change sigma^2
    by < 2 %; clip_mode pixel reads ~15 % lower with the same fit quality.
    Relative area at 0.135 is monotonic but scatters up to 18 % between repeats;
    at 0.5 it is flat near focus, not monotonic, and its minimum is ~1 um off.
- Autofocus on the real (open-loop, hysteretic) KIM Z, 5 runs per start
  (2 focal depths below / above / far below; focal depth ~1.2 um), judged by
  D4sigma at the park against its minimum (the Z counter is not a ruler here):
  - one_way + spot_d4sigma (park_tolerance 0.04): 14 of 15 parked at 29.7 --
    30.5 px (~0.2 focal depths), parked D4sigma scatter < 1 %.
  - one_way + spot_relative (0.10): 15 of 15 parked, all at 32.6 -- 34.7 px
    (~0.5 focal depths): the tolerance is loose for a metric that is flat near
    focus.
  - one_way + spot_area on an UNSATURATED spot: 0 of 15 -- 14 parked
    consistently at ~46 px (1.2 focal depths). The metric is minimised, which
    assumes a saturated spot; unsaturated, the thresholded area is LARGEST at
    focus, so the routine goes to where the spot fades. Not tested at the lab's
    saturating exposure.
  - sweep routine: parks 0.2 -- 1.0 focal depths off, worse run by run: the
    final down/up approach to the park does not land where the sweep was.
  - A run that never gets back within park_tolerance parks by the step
    counter -- up to 3.7 focal depths off on this Z -- and still reports `OK`
    (the warning goes out as an event only). Seen 5 times in ~90 one_way runs.
  - During the test the Z counter at focus climbed from -1.5 to +344 um: kim's
    Z steps UP are now much smaller than steps DOWN (one step size in the
    config). Only a routine that parks by the image works on this axis.
- 2026-09-29, after 429497c / 6da4e31 (Z kept within +-20 um of a hand-set
  focus by kim's Z leash, 650 steps, re-zeroed at every image-confirmed focus):
  - The "+344 um" Z drift of the day before was COUNTER micrometres (steps x the
    default 20 nm): +17 300 steps net over ~110 runs whose parks were all at the
    same focus by the image -- the up/down step imbalance, not a physical move.
  - 429497c: a park that never gets within tolerance now FAILS ("park failed")
    and returns Z to the start -- seen on the rig. It failed there because the
    target was a single 8-bit noise dip (51.2 against neighbours of ~56 px^2).
  - 6da4e31: one_way + spot_d4sigma parks against the FITTED fine-walk minimum
    with a noise-aware tolerance (2 x the measured 3 -- 10 %/level, capped at
    10 %): 5/5 OK in 14 -- 16 s from 2 focal depths below, D4sigma at park
    29.2 -- 30.2 px (minimum ~29.6); spot_encircled and spot_gauss 1/1 each
    (Gauss parks slightly lower, D4sigma 32.3 px).
  - autofocus.exposure_us: 2480 -> 65 us during the run and restored after it,
    also after a kill. "Auto exposure (once)" = the camera's ExposureAuto=Once
    (2454 -> 2480 us), which returns to Off by itself.
  - Six sizes through Z (65 us, unsaturated, +-5 um): optima within 1 um of
    each other (Gauss sigma^2 and relative area at 0, peak +0.25, D86 +0.5,
    D4sigma +0.75 -- 1.0 with a broad flat minimum). Gauss fit R^2 0.98 -- 0.99
    near focus, 0.63 -- 0.76 at +-5 um, where its sigma^2 blows up.
  - Calibrate spot "unsaturated" (at the AF exposure): 0.12 -- 0.16 px jitter,
    0.7 px from the old calibration. "saturated" at the working exposure
    ACCEPTED a result 92 px off with 277 px jitter: the fixed threshold also
    takes the saturated illuminated block in this field of view.
  - Locating the spot: "peak" is right at 65 us (1.4 px; finds a 50 px-off
    calibration); "blob" finds nothing at 65 us and is 53 px off at the working
    exposure (a false "moved" warning). At the working exposure the "why" text
    pointed at the illuminated block, not at the laser.
  - PixelFormat Mono8: the spot metrics run on 8-bit frames (the info line
    says so); Mono10/12 not yet tried.
  - Re-checked after 9da7a07 (every spot search stays inside the search region
    around the calibrated laser): locate = blob at 65 us finds the spot (0.6 px);
    at the saturated working exposure blob and peak no longer land on the
    illuminated block -- they report nothing ("1 larger than max area,
    1 elongated") instead of a wrong position; the why-text at 65 us names the
    laser 50 px from a hand-shifted calibration. Calibrate as "saturated" at the
    working exposure: (973.39, 464.84) +- 0.94 px, 0.3 px from the unsaturated
    calibration; with the calibration moved far away it refuses. An
    "unsaturated / at the AF exposure" calibration with autofocus.exposure_us = 0
    (not set in camera.ini) refuses correctly ("jumps by 99 px between frames").
    Two one_way + D4sigma runs: OK, 30.3 / 29.7 px; the AF itself no longer
    repeats the SATURATED info line.
  - After 70265b7 (the saturated laser at the working exposure, built on real
    frames saved in the private lab repo; its 21 real-frame tests ran on the lab
    PC): with max_area_px = 0 (automatic, a quarter of the search region) blob
    and peak land on the laser at 2480 us, 0.7 px from the calibration, and the
    live size has a value. camera.ini's max_area_px = 2000 still rejects it (the
    laser + rings is 3.4 -- 7.8 k px^2 at +-2 um), with a message saying so. No
    offset warning over four 65 <-> 2480 us switches; calibrating "at the AF
    exposure" with autofocus.exposure_us = 0 warns and calibrates at the
    working exposure (+- 0.34 px); why-texts appear once.
  - After 54c76de (GUI): with camera.ini's max_area_px 2000 and the spot 2 um
    out of focus, status gives the reason ("the blob at the calibrated position
    is 3047 px, larger than max area 2000 px -- set max area to 0 ..."). One
    one_way + D4sigma run with zoom_on_af: the view zoomed to the spot region
    with its label and a "Whole frame" button during the run, and went back to
    the whole frame after it ("Zoom to spot region" button); parked at D4sigma
    29.3 px. Not checked here (needs the mouse): wheel scrolling of the
    four-column AutoFocus tab, double-click during a run, the zoom button.
  - Z step calibration by the camera (1b4e63b; ratio from the WIDTH of the
    sigma^2 curve at 1.3 / 1.5 / 1.8 / 2 x its minimum, up walk vs down walk),
    twice at 63x with the walk capped at 8 um: up/down step ratio 0.743 +-
    0.003 (levels 0.742 / 0.755 / 0.744 / 0.741) and 0.804 +- 0.002 (0.800 /
    0.805 / 0.803 / 0.810) -- 8 % apart; the parabola-fit ratio repeats
    better (0.784 / 0.787; R^2 0.94 -- 0.96, below the old 0.97 gate). kim then
    moves Z by ~17.9 nm per step up, ~22.3 nm down. After it, the sweep routine
    (D4sigma) parks at focus -- 30.2 px, against 0.2 -- 1 focal depths off the
    day before -- and one_way at 30.1 px. The live spot size keeps updating
    during the walk; the zoomed view shows the spot (stretched display) during
    an autofocus.
  - After d108b96 + b7885c6: a third calibration (ratio 0.691 +- 0.015, level
    spread 8.8 %; the three runs 0.743 / 0.804 / 0.691) ends "saved by kim to
    ...kim.ini" and parks Z in focus by the image (D4sigma 30.2 px). After a kim
    restart the Z step sizes come from kim.ini (up 0.01663, down 0.02405 um/step)
    and the controller's drive settings are untouched (85 V, 1500 steps/s,
    20000 steps/s^2, counter kept). During an autofocus at the AF exposure the
    view label reads "threshold check paused (autofocus exposure)".
  - 2026-10-08, after fd044a0: drawing a SECOND, different template keeps the
    scan array where it was on the image (array centre (1188, 636) px, the new
    template at (1585, 221), the spot at (972, 465); before the fix the array
    jumped onto the spot / the template). Test:
    [`test_redraw_template.py`](../modules/imaging/camera-control/tests/test_redraw_template.py).
    Found on the way: after a camera-service restart the open GUI's "Allow
    tracking" box still showed ticked while the new service had tracking off
    (the box is never read back from the status) -- untick + tick again. Fixed
    in f50172f: with tracking + stabiliser on, the service was restarted under the
    open GUI, and a few seconds after it came back "Allow tracking", "Stabilise"
    and "Continuous focus" were all unticked, matching the new service. Test:
    `test_gui_smoke.py::test_on_off_boxes_follow_the_service`.
- Not yet checked on the rig: the lost-pattern fault of 2026-09-28, 12-bit spot
  frames.

## signalhound -- Signal Hound SA44B + USB-TG44A

**Checked (2026-09-28)**, the TG only through a 20 dB attenuator at -30 / -20 dBm:
- Identification: SA44B, `sa_api` 3.2.4 from Spike.
- Spectrum mode through the backend and the real service: the frequency grid read
  back for RBW 10 Hz .. 250 kHz, fresh sweeps, detector arrays, `acquire` over the
  wire (a 1 GHz tone read at -50.2 dBm).
- Clean `shutdown` and restart, and `check_modules --live`.
- The tracking generator measured through the raw API: CW level and frequency,
  CW during spectrum sweeps, TG sweeps, switching times.

**Commits:** [cfc8d6e](https://github.com/FlashLukas/AaltoFlow/commit/cfc8d6e),
[b8653a2](https://github.com/FlashLukas/AaltoFlow/commit/b8653a2),
[a66c185](https://github.com/FlashLukas/AaltoFlow/commit/a66c185).
**Tests:** [`tests/test_sa_api.py`](../modules/detector/signalhound-control/tests/test_sa_api.py)
(against [`fake_sa_api.py`](../modules/detector/signalhound-control/tests/fake_sa_api.py)).

**Caveats found on the hardware:**
- `sa_api.dll` ships with Spike and is found in Spike's folder; opening takes 3.7 s.
  "Device not found (-8)" usually means another service or Spike holds the
  analyser.
- The API never reports a compression warning on this unit; overload is judged
  from the trace against the reference level.
- A 150 kHz RBW is accepted silently and gives the 100 kHz grid.
- Sweep time is ~0.05 s + span / 135 MHz/s + 10 us per output bin (the bins
  carry the cost of a narrow RBW); within ~2x of every measured sweep, e.g. 50 MHz
  -- 4.35 GHz in 32 s, RBW 10 Hz over 100 kHz in 0.68 s. The first estimate was off
  by up to 1000x.
- **The TG44A has no "off".** It keeps emitting its last frequency and level after
  an abort, a close and even the program exiting; only a new setting or unplugging
  changes it. The suite's plan is to "switch it off" by PARKING it at 10 kHz,
  -30 dBm -- designed, not yet tried on the hardware.
- Reading back the TG's frequency/level only echoes what the same program set --
  its state at start is unknown.
- A CW from the TG coexists with spectrum sweeps (level honoured: -30 dBm read
  -50.04 dBm after the 20.0 dB pad).
- In a TG sweep the level setting is IGNORED and the trace is transmission in dB
  relative to the TG output, not dBm (-19.4 dB flat, 900 -- 1100 MHz, through the
  20 dB pad); at most 1001 points (more are cut to 1001 silently), ~0.2 s + 1.3 ms
  per point.
- A second service on the same analyser (serial 0 = "first found") cannot even open
  it: it stops with "Device not found (-8)" (now with the hint above) before the
  hardware lock is reached. With a serial configured, the lock answers first.
- **The three-module chain on the hardware (2026-09-28)** -- signalhound `--real`,
  shsg `--real`, shsna `--real`, each its own process, TG -> 20 dB -> SA:
  - shsg CW seen by the SA: 900 MHz at -30 / -20 / -10 dBm read -50.09 / -40.07 /
    -30.15 dBm (the -10 dBm top end works), 2.5 GHz -30 read -51.23; frequency
    within a 1 kHz-RBW bin; each setting ~0.02 s.
  - RF off = park: the 2.5 GHz tone disappears, a -49.5 dBm tone appears at
    10.04 kHz (i.e. -30 dBm), shsg reports `parked`. Harmless on the pad + SA.
  - shsna 800 -- 1200 MHz, 201 points: `tg_grid`'s PREDICTED grid (800 MHz,
    2 MHz bins, 201 points) equals the real TG sweep's grid. Reference 1.43 s,
    measurement 1.22 s; transmission after the thru reference: mean +0.017 dB,
    peak +0.087 dB. The raw TG-sweep trace read ~-22 dB through the pad, the
    same at 1 kHz and 100 kHz RBW; the -19.4 dB of the earlier raw-API test came
    from something else in that configuration, not from the RBW.
  - shsg's CW comes back after the exclusive TG sweep (900 MHz -50.07 dBm).
  - A CW change sent while the SA is inside a long sweep (7.5 s, 1 kHz RBW,
    1 GHz) first failed at the client after 1.5 s although it was applied later;
    fixed in 0bce28a and re-checked the same day: `ok` in 0.21 s, shsg keeps the
    old frequency until the sweep ends, then shows the new one, and the SA sees the
    tone there (950 MHz, -50.17 dBm).
  - shsna windowed acquisition (4faa7b6) over the thru: `window_fallback` stays
    empty and the sub-band's bins land on the full grid (a window of bins 95 --
    105 of 201 measures exactly those 11), transmission ~0 dB inside.
    Time per acquisition, back to back, at the client (owner total in brackets):

    | code | 11 bins (window) | 201 bins (full) |
    |---|---|---|
    | 4faa7b6 | 1.26 -- 1.36 s | 1.77 s |
    | d40ba9d | 1.03 -- 1.14 s (0.85 -- 0.95) | 1.48 -- 1.57 s (1.35 -- 1.38) |
    | eb165ba | **0.61 -- 0.63 s** (0.59 -- 0.60) | **1.08 -- 1.11 s** (1.07) |

    With eb165ba the owner's breakdown is queued 0.00, configure 0.02, sweep
    0.49 -- 0.50 s (11 bins) / 0.98 s (201 bins), restore 0.07 s, and the client adds
    only 0.02 -- 0.04 s: what remains is the SA44B's TG sweep itself (~0.45 s fixed
    + ~2.6 ms per bin). The SG's CW survives every TG sweep and the later spectrum
    reconfigure (-50.06 dBm at 900 MHz before and after).
  - A thru reference taken at another RBW is refused with a clear message
    (7652982, re-checked).
  - Clean `shutdown` of all three.
- Not yet checked: `saStoreTgThru` (VERIFY 6), a real DUT in the SNA path, and
  two SAs on one PC.

## dssg -- DS Instruments SG12000L microwave generator (2026-10-07)

Checked against a Signal Hound SA124B through a 30 dB pad.

- **Vernier** (`VERNIER n`): the unit takes -800..+100 counts and clamps
  outside that SILENTLY (no error); + = more power; `POWER` / `FREQ` changes
  keep it; `POWER?` excludes it. ~0.045 dB/count near 0 at 1-4 GHz, but
  frequency- and power-dependent (a resonance near 6 GHz) and non-linear far
  out. The module's limits are the measured range.
- **The attenuator's 0.5 dB steps are not accurate above ~4 GHz**: up to
  0.8 dB off at 4-7 GHz and 1.2-1.7 dB at 8-12 GHz (relative to the unit's own
  -10 dBm). Fine power (the vernier fills the steps) plus the per-unit power
  calibration (`scripts/calibrate_power.py`, 22 frequencies x every step,
  2 passes, ~30 min) fixes it: verified within +-0.07 dB at 1, 4, 6, 10 GHz and
  2 GHz / -20 dBm. The steps repeat only to ~0.1-0.2 dB between runs, so
  ~0.2-0.3 dB is the honest promise.
- An off-step `POWER` value is ignored by the firmware (fine power never sends one).
- Caveats: the calibration is per unit (a gitignored JSON on the lab PC); at
  12 GHz the low-power rows are near the analyser's noise floor (spread 0.57 dB).

## afg -- Tektronix AFG1062 (2026-10-07)

Checked on the scope (AFG CH1/CH2 -> scope CH1/CH2, high-Z).

- Levels 10 mVpp..20 Vpp within the scope's ~2-3 %; offsets to the 10 V peak
  limit; clamps with clear warnings; amplitude + offset fitted as a pair
  (order-independent).
- **Phase**: the unit keeps WHOLE degrees and truncates; bare numbers are
  radians; negative phases are refused (error -201 "Invalid while in local").
  The module sends `<deg>DEG` normalised to 0..360 and compares modulo 360.
- Frequency / phase follow (CH2 from CH1) including front-panel changes;
  follow settings kept in afg.ini across restarts; ~0.6 s per change.
- Not available remotely on this firmware: ramp symmetry, noise, DC level
  (refused with the reason).
- `shutdown{keep_outputs}` leaves the outputs as they are (restart).

## scope -- RS PRO RSDS1102CML+ / Siglent SDS1000CML+ (2026-10-07)

- Records over USB (binary block, no terminator), trigger delay sign, both
  channels, physical units saved in scope.ini, XY view.
- **Slow timebases**: in NORMAL trigger mode records keep coming (one per
  record length); in AUTO at >= 50 ms/div the scope rolls -> acquire refused
  with the reason. The read timeout follows the record length (a too-short one
  wedged the USB once: power cycle needed).
- `INR?` blocks ~0.5 s while running, so new records are found by comparing
  the data (with one `INR?` as a fallback for identical records): 16 averages
  at 1 ms/div in ~4.5 s.
- Numbers (pk2pk, frequency, phase CH2-CH1) come from the FULL record; the
  stored trace is reduced without inventing a signal (an alias warning when it
  has < 4 points per period).
- Only some time/div values exist on this model (20 ms -> 10 ms); the module
  warns when the scope changed a value.

## scan-core -- scans with real instruments

- 2026-09-15: wavelength scan with acquired pm16 detectors (see pm16).
- 2026-09-25: a 21 x 25 reflectivity map with kim moving and pm16 measuring; it
  showed the open-loop drift described under kim.
- 2026-09-28: **fly scans on the rig** (kim + pm16, and camera + kim + pm16):
  - in kim coordinates (X flown +-5 um, 2 rows, 2 um/s, zig-zag): found and fixed a
    skipped approach to the first row and a speed round trip between zig-zag rows
    ([fc73ad1](https://github.com/FlashLukas/AaltoFlow/commit/fc73ad1),
    [a5a59a6](https://github.com/FlashLukas/AaltoFlow/commit/a5a59a6),
    [9cdbf79](https://github.com/FlashLukas/AaltoFlow/commit/9cdbf79)); afterwards
    every pixel filled (3 -- 8 samples), row gap 0.48 s;
  - in camera coordinates (laser x flown -10 .. 10 um, 100 px, 1 um/s; 5 rows placed
    by the camera): 5 x 100 px in 140 s, no empty pixel, rows straight to ~0.035 um
    (sd of the measured laser y); zig-zag halves the gap between rows (2.7 -- 3.8 s
    vs 4.8 -- 6.2 s); forward/backward rows agree to 0.085 um at 1 um/s and 0.005 um
    at 0.5 um/s (inside the row-placement scatter: no measurable lag); flying Y
    works, the direction is learned by itself.
  Tests: [`test_flyscan.py`](../scan-core/tests/test_flyscan.py),
  [`test_fly_wire.py`](../scan-core/tests/test_fly_wire.py) (incl. the lagging
  position read found on the rig), [`test_fly_camera.py`](../scan-core/tests/test_fly_camera.py).
  Caveats: rows are placed ~0.04 -- 0.08 um short of the target on the side they
  come from (even with "stable within" 0.05 um); kim's step counter drifts ~2.7 um
  per row against the sample, which flying in camera coordinates absorbs; with
  0.1 um pixels at 1 um/s only 1 -- 3 pm16 samples fall into a pixel.
- 2026-10-08: **XY mask on the rig** (camera + kim + pm16,
  [609619c](https://github.com/FlashLukas/AaltoFlow/commit/609619c)): the camera's
  30 x 28 scan array (camera.scan_ix / scan_iy, placed by the stabiliser) across a
  dark-film / bright edge; pass 1 = pm16 at every 3rd point (10 x 11), automatic
  threshold, keep the bright side, automatic margin (1.5 points). 203 of 840 points
  measured (24 %), every one of them finite, none outside the mask; the pass-1 map
  matches the camera image; of the 39 measured points on the mask's boundary only 1
  is above the threshold, so no bright rim was cut off. 13.2 min in all (pass 1
  5.4 min, pass 2 7.9 min at 2.3 s per point) against ~32 min for the full grid.
  Tests: [`test_mask.py`](../scan-core/tests/test_mask.py),
  [`test_mask_builder.py`](../scan-core/tests/test_mask_builder.py).
  Caveat: at a row change the outer axis (Y) is set before X, so the camera first
  settles at the corner below the row's last point (not recorded) -- one extra
  settle per row, in pass 1 and in the stepped scan alike. Fixed (opt-in) by
  `diagonal: true`, below.
- 2026-10-08: **diagonal row change on the rig**
  ([01b9b6a](https://github.com/FlashLukas/AaltoFlow/commit/01b9b6a)), the same
  masked camera-array scan with `diagonal: true`: the camera goes from (29, row)
  straight to (0, next row) -- logged from its status, (29, next row) never
  selected, and seen in the GUI. Pass 1 279 s instead of 321 s (~4.8 s per row
  change), pass 2 2.13 s instead of 2.32 s per point; 199 of 840 points, 11.7 min
  in all (full diagonal grid ~30 min). Test:
  [`test_diagonal.py`](../scan-core/tests/test_diagonal.py). Not checked: two KIM
  axes moved at once (kim.position_x/y as the raster axes).
- Not yet on hardware: fly scans with hf2, the pause-on-fault of 2026-09-28, a mask
  loaded from a file (`from:`).

## Simulation only (no hardware pass yet)

agilis, ccs200, chopper, clMag, cs260, ddr25, dsamp, dsphase, elliptec,
gsp818, hf2, hp8648, k2450, kepco, ls455, mag2d, mag2dcal, piezo, pm400, ppms,
smaract, smb, sr7230, sr830, stage, superk, tc200, usb6001, vna,
windfreak, zpiezo. The per-module hardware checklists are in
[`DEVELOPER_NOTES.md`](DEVELOPER_NOTES.md) section 11 and each module's README.
