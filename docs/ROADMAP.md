# Roadmap -- parked ideas and open work

What has been discussed and deliberately left for later, with enough of the
reasoning that it can be picked up without the original conversation. Newest
first within each section. When an item is done, delete it here and describe
the result where it belongs (README, developer notes, guide).

## To discuss

### Data types: recording, storing and processing more than float64 (2026-10-02)

Items 1-4 (typed storage from describe, float32 opt-in, compression, enums
and strings) were BUILT on branch `data-types` (2026-10-04, not merged yet):
see `docs/DEVELOPER_NOTES.md` section 4b and `scan-core/scan_core/storage.py`.
Still open: AaltoView showing enum NAMES (it shows the codes; the names are in
`flag_meanings` / `options_json`), modules adding `min`/`max`/`bits` to the
detectors where size matters, and item 5 (ragged data), only if needed.

### dssg: show the MEASURED harmonic content in its window (2026-10-03)

The DS Instruments generator is unfiltered, so its harmonics are real.
Today the window's little spectrum (`apps/gui.py`, `SpectrumIndicator`)
draws the 2nd and 3rd harmonic at fixed "typical" levels (-25 / -35 dBc,
hard-coded: "illustrative, not a measurement"). Plan: measure them once and
show the real numbers.

- **Measure:** a scan of the generator against a spectrum analyser -- dssg
  frequency (and power, if the harmonics depend on it) as axes, the
  analyser's trace as the detector, as in the 512 x 21 000-point
  signalhound map. A ready-made recipe in `scan-core/recipes/` makes it
  repeatable. Note the analyser's range: the SA44B stops at 4.4 GHz, so a
  2nd harmonic is only measurable for carriers up to 2.2 GHz, a 3rd up to
  ~1.47 GHz; above that the table says "not measured" (or a second
  instrument fills it in).
- **Extract:** a small tool (or an AaltoView analysis) reads the .nc, finds
  the peak within a window around n x f for n = 2..N (plus the carrier, for
  the reference), and writes a harmonics table in dBc against frequency
  (and power) -- a file next to the module's config, picked up by "Export
  settings". No serial numbers in tracked files.
- **Use it in dssg:** the brain loads the table and interpolates (log f,
  power) for the current setting; status and describe gain indicators
  (`h2_dbc`, `h3_dbc`, ...) a scan can record next to its data; the GUI
  draws the measured lines with their levels ("2nd -31 dBc, measured
  2026-..") and falls back to the typical values, marked "typical", outside
  the measured range or with no table.
- Later, the same for the other generators (smb, hp8648, windfreak) -- the
  table format and the extraction tool are generic.

Effort: ~1-1.5 days (extraction + table + dssg indicators + GUI + tests with
a simulated measurement), plus the measurement itself.

### Oscilloscope module (first: Digilent Analog Discovery 3) for MOKE hysteresis loops (2026-10-03, spec agreed)

**2026-10-07, Lukas: the scope module is a plain SCOPE** -- the loop
analysis (Hc, Ms, ...) below was removed from it and belongs in the AaltoView
processing module; the scope records the traces (and keeps the XY view).

**BUILT 2026-10-06** as `scope-control` (simulation + the Siglent SDS1000CML+
backend, untested on the instrument). The Siglent backend and the Digilent
(dwf) backend -- scope + W1/W2 generator + V+/V- supplies + T1/T2 trigger --
are on the real instruments since 2026-10-07/09 (Analog Discovery 2; the AD2's
self-test passes; supplies still to be tried). Still open from this spec: the
drift number, trigger holdoff, the live map of traces against a scan axis.
The spec's open points were decided as written in the module's README.

**Purpose.** Classical laser MOKE hysteresis loops: the magnet is driven
continuously (~30 Hz); one scope channel measures the field (Hall probe) or
the magnet current, the other the light intensity; the loop is channel 2
against channel 1 (XY). A scan point = one averaged loop, so loops can be
recorded against anything else in a scan (position on the sample via the
camera's scan points -> spatially resolved loops, temperature, angle, ...).

**Waveform generator first (2026-10-06):** `afg-control` (Tektronix AFG1062,
simulation only) has a GENERIC generator brain (`generator.py`, backend
`capabilities()` + `envelope()`), written so this scope module can copy it
for the AD3's W1/W2. Bench wiring then: AFG CH1 -> scope CH1, AFG CH2 ->
scope CH2 and EXT TRIG. The scope on that bench is an RS PRO RSDS1102CML+
(a rebranded Siglent SDS1102CML+): the SCPI-over-VISA backend below.

**A generic N-channel scope, not a Digilent module.** Key e.g. `scope`,
category detector. The brain asks its backend for CAPABILITIES and builds
everything from them: number of scope channels, buffer size and sample-rate
range, generator channels (0..n), digital I/O, power supplies, trigger
inputs. `describe`, the GUI and the safety verbs follow the capabilities --
the Generator tab is shown but greyed out when the device has no generator,
likewise Supplies. First backend: Digilent WaveForms SDK (`dwf` library,
installed with WaveForms; ctypes or a wrapper such as dwfpy), the only file
that imports it, lazily, every unverified call marked `# VERIFY`. Later: an
SCPI-over-VISA backend (Rigol / Keysight / Tektronix) with the same
describe. Device claimed with hwlock by its serial number.

AD3: 2 scope channels, 14 bit, up to 125 MS/s, 32 Ki samples per channel;
2 waveform generator channels; 16 digital I/O; trigger pins T1/T2;
programmable supplies. (VERIFY each against the SDK on the device.)

**Trace = the saved, recorded acquisition parameters** (in the config, in
every scan's file):
- time base: number of points (e.g. 1024), time span, time offset
  (trigger position);
- per channel: on/off, range, vertical offset, MULTIPLIER, offset and unit
  for the physical quantity (V -> mT for the Hall probe, V -> A for a shunt,
  intensity in V or a.u.), so traces and loops are stored and shown in
  physical units with the conversion recorded;
- these must not change during a scan (the engine already refuses a
  changed trace length); they can be swept only as an outer axis.

**What a scope does (Scope tab):**
- trigger: source (any channel, external T1/T2, the generator's own sync,
  a digital line), level, slope, hysteresis, mode (auto / normal / single),
  holdoff, position;
- views: Y-t (all channels, time axis) and XY (choose X and Y channel -- the
  hysteresis loop), cursors;
- averaging: a running average shown live (number N, "Restart average"
  button, count shown "312 / 500"); for an acquisition (a scan point) the
  average is RESTARTED and the point completes when N fresh triggers are in
  -- 500 averages at 30 Hz = ~17 s per point, which the scan's wait block
  and the ETA must know;
- digital filter: low-pass and high-pass (cut-off, order), ZERO-PHASE
  (forward-backward) and applied identically to every channel -- a phase lag
  between the field and the intensity channel would tilt / open the loop.
  Recorded data are filtered with the settings stored next to them; the raw
  (unfiltered, averaged) traces can be recorded as well.

**Generator tab (basic, both channels):** waveform (sine, square, triangle,
ramp, DC), frequency, amplitude, offset, on/off; channel 2 locked to
channel 1 (same frequency, a phase offset) -- the use here: W1 = the sine
for the magnet amplifier, W2 (or a digital line) = a synchronized square
into the external trigger input of the other instrument. Optional supplies
(on/off, voltage). These are controls, so scan axes too (e.g. amplitude
for minor loops). Nothing more elaborate for now.

**Safety verbs:** `generator_off` (both outputs -- it drives the magnet),
`supplies_off`, `stop` (abort the acquisition). Describe actions as usual.

**Recorded in a scan (detectors):**
- traces: one per enabled channel, sharing the time axis (2 x 1024 x 8 B
  = 16 KB per point -- small);
- computed values per channel: mean, RMS, peak-to-peak, amplitude,
  frequency, phase between channels;
- **loop analysis** (proposed -- to judge which are worth keeping): the loop
  split into its up and down branches by the X direction; coercive fields
  Hc+ and Hc- (zero crossings of Y about its mid level), Hc = (Hc+ - Hc-)/2
  and the exchange-bias shift (Hc+ + Hc-)/2; saturation levels and the Kerr
  amplitude (half the jump between them); remanence Mr (Y at X = 0) and
  squareness Mr/Ms; loop area; a linear background (Faraday / substrate
  slope) fitted in the high-field ends and subtracted, with the slope
  recorded; normalised loop (-1..1) as an option; drift = the loop not
  closing between start and end of a period.

**Display in the suite:** the latest trace / loop at the current point; a
map of traces against a scan axis (needs the "narrow features" live-map fix
above for sharp switching).

**Simulator:** the generator drives a simulated magnet (30 Hz sine, field
with noise on X), Y = a hysteresis loop (tanh-shaped branches with Hc, Ms,
a Faraday slope, noise, slow drift); the sync square and the trigger behave
as on the device, so averaging, trigger, filter, XY and the loop numbers
are all testable offline.

**Effort:** backend + brain + service with capabilities ~2 days; GUI
(Scope Y-t / XY, trigger, averaging, filter, Generator / Supplies tabs)
~2 days; loop analysis ~1 day; scan integration, simulator and tests
~1-1.5 days -- about 6-7 days, plus a hardware session with the AD3.

**Open:** which instrument gets W2's sync (its trigger input level); whether
filtering should also apply to the live display only. (Decided 2026-10-07:
the running average is the mean of the last N traces, not exponential.)

### Scientific cameras for spectroscopy (2026-10-02)

CCD / sCMOS cameras on a spectrograph (Andor, Teledyne Princeton
Instruments, Hamamatsu). They fit AaltoFlow well:

- Full vertical binning gives one spectrum per point (1024-2048 values) --
  the 1D-detector path scan-core already has. Multi-track gives a few x 2048;
  image mode at most 2048 x 512 x 16 bit = 2 MB per frame.
- `ccs200-control` is the template (spectrum detector, wavelength axis from
  the instrument, acquire-and-wait, Take dark, Abort):
  `python tools/new_module.py <key> --like ccs200`.
- `pylablib` (already used for Thorlabs Kinesis) wraps Andor SDK2/SDK3,
  PICam, PVCAM and Hamamatsu DCAM behind one interface: one module, a backend
  per maker.
- The module needs: readout mode (FVB / multi-track / image) setting the
  detector's shape; exposure x accumulations + readout as the acquire
  timeout; dark / background with the shutter closed; cosmic-ray removal;
  sensor temperature, with "cooled and stable" as a fault a scan waits on;
  safety verbs abort + close shutter; gain, readout speed, EM gain and
  binning recorded with every scan.
- The spectrograph (Andor Shamrock / Kymera, PI IsoPlane) is its own module;
  its grating, centre wavelength and slit become scan axes, and the camera
  takes its wavelength axis from the spectrograph's calibration.
- scan-core: a live 2D view in the Measurement tab (~0.5 day); 16-bit
  storage (above) for large image-mode maps.

Effort: camera module with a simulator and tests ~3-4 days, spectrograph
module ~2 days, plus a short hardware session per maker.
**Needs first:** which camera and spectrograph (maker, model).

### Camera images as a scan detector (2026-10-01)

PHASE 1 DONE 2026-10-10 (simulation only), and the cheap half of phase 2:
`camera.image` (full / spot crop / rect, binning, numbered fresh frames,
auto exposure / gain held off during a scan), binary reply parts, uint16
storage one frame per chunk with `<det>_measured`, write-as-you-go above
1 GB, the size in the Scan tab, the newest frame next to the map. See the
camera README "Images for scans", the scan-core README "Images", developer
notes 4 + 4b, guide 6b "Images" / "Binary replies".

Still open:
- **On the rig**: how many frames the IDS camera has queued after a request
  (`record_discard_frames`, default 1, # VERIFY); whether its ExposureAuto /
  GainAuto exist as GenICam features at all; real frame rates of a scan.
- **Phase 2, the rest**: the scan server's watchers do not get the frames of
  a big map (left out of the mirror above 32 MB; the file is on the server's
  PC); a FILE written in place (big maps) is not atomic -- a crash during a
  checkpoint write could damage it; maps above RAM in AaltoView (it loads a
  variable whole: a 20 GB file needs lazy/dask loading there).
- A single snapshot as a detector of a 0-D scan works already (a scan with
  one point); a "take one picture into the run's file" routine step does not
  exist yet (save_picture writes a PNG next to it).

## Open work

- **Fly scans over any knob -- the remaining modules** (the contract and three
  pilots DONE 2026-10-09: a `ramp` block in describe, guide 6b "Ramps";
  `suite_common/softramp.py`; clMag field, dssg frequency, ppms field).
  Lukas: "when it makes sense". DONE 2026-10-10 (simulation only; each
  README has a "Sweep" section and its `# VERIFY` list):
  * **mag2d / mag2dcal**: DONE 2026-10-10 (simulation only) -- field
    magnitude (`ramp_field`) and the ROTATING ANGLE (`ramp_angle`, the
    angular FMR scan as a fly axis), the control loop walking the setpoint,
    binned by the MEASURED field / angle (Hall stream); mag2dcal has a SWEEP
    state in which the freeze and the stabilizer stand aside and the drive
    never steps back (clMag's model, gotcha #11).
  * **kepco**: DONE 2026-10-10 (simulation only) -- current (`ramp_current`,
    softramp.py at A/s), binned by the MEASURED current; the voltage limit,
    output off and the watchdog stop it; it never switches the output on.
  * **smb / windfreak / hp8648 / shsg / dssg**: DONE 2026-10-10 (simulation
    only) -- software ramps via softramp.py on frequency, power and phase
    where the instrument has them (smb, windfreak per channel, dssg: all
    three; hp8648, shsg: frequency + level, no phase control). Every knob is
    binned by COMMAND (measured false): FREQ? / POW? and the shsg owner's
    echo return the stored setting, not a measurement, and a query per step
    would halve the step rate. dssg sweeps power only with fine power (each
    step re-splits attenuator + vernier, calibrated). Verbs ramp_<knob>,
    ramp_stop{knob?}; status <knob>_ramping / <knob>_ramp_id; one stream
    group "ramp", a channel per knob (own stamps in t_ch); a Sweep card in
    each GUI. Open: the per-step timings on the instruments (# VERIFY in
    each module), and the signalhound owner logs one event per TG step.
  * **k2450**: DONE 2026-10-10 (simulation only) -- `ramp_voltage` /
    `ramp_current` as SOFTWARE ramps (the 2450's own :SOUR:SWE is a
    trigger-model list the poll cannot read during), binned by the measured
    readback; compliance stops a sweep; it never switches the output on.
  * **chopper**: DONE 2026-10-10 (simulation only) -- `ramp_frequency`
    (softramp.py), binned by the MEASURED wheel frequency (REF OUT on a
    sensor), honestly by the commanded one while REF OUT is on 'target'.
  * **tc200** temperature: a SOFTWARE ramp of the setpoint in the box's
    0.1 degC steps (its own ramps live only in the front-panel CYCLE program,
    no confirmed serial commands); binned by the MEASURED temperature, polled
    fast while sweeping; C/s on the wire, K/min in the GUI.
  * **ppms temperature**: a HARDWARE ramp (MultiVu, fast_settle; K/s on the
    wire, K/min to MultiVu); arrival = within tolerance_K and Near/Stable; its
    own done keys (`temp_ramping` / `temp_ramp_id`) and stop verb
    `ramp_temperature_stop`. The stream group is now `cryostat` (field AND
    temperature in one recorder, so a fly starts/drains it once per row).
  * **superk** wavelength: a SOFTWARE ramp per AOTF line, one sweep at a
    time, binned by command; one stream group `ramp` with all 8 lines
    (forward-filled); a sweep never switches emission.
  * **elliptec** angle: a HARDWARE ramp ("move to at velocity", deg/s -> the
    ELL14's velocity percent, the user's velocity restored afterwards),
    binned by the polled encoder angle. Fly rows are fast: the slowest speed
    is ~30 % of ~430 deg/s.
  * **afg** and the **AD2** waveform generator: DONE 2026-10-10 (frequency /
    amplitude / offset / phase per channel, software ramps, measured false;
    the follower follows each step). Simulation; the lab tries them on the
    bench.
  Looked at and SKIPPED (2026-10-10), with the reason:
  * **ls455**: a gaussmeter -- its field is a measurement, not a setting.
  * **cs260** monochromator: no scan / speed command, GOWAVE blocks the bus
    until the grating has arrived, the mechanical arrival time is unknown,
    and a filter change blanks the light -- stepped scans stay.
  * **agilis**: open loop with no speed control; it already streams its
    counted position.
  * **ddr25, smaract**: need nothing -- they fly on the STAGE path already
    (position stream + a velocity in unit/s; `check_modules ddr25 smaract
    --live` passed 38/0).
  Still open: **piezo, zpiezo** -- those that stream a position fly on the
  STAGE path already (speed_param); the rest need a stream + a speed knob.
  The Scan Builder side is DONE (2026-10-09, 7ab8757: the FLY group of the
  per-axis Advanced panel offers fly on a ramp knob). The three pilots, the
  five modules above (2026-10-10; every rate limit and bus budget is a
  `# VERIFY`) and the VNA streaming (traces + single points, 2026-10-09) are
  simulation-only until their instruments are connected again.

- **Scan server** -- phase 1 DONE 2026-10-05 (the scan engine as a service,
  `scan_core/scan_server.py`; watch / abort / answer from any PC), MIRROR
  DONE 2026-10-06, **phase 2 DONE 2026-10-10** (scan-core README "The scan
  server", developer notes 4f): define and submit from another PC under the
  CONTROL rule (the "same PC only" rule is gone), the run info of the
  submitting PC, the server's registry and live limits decide; editing a
  running queue (`queue_add` / `queue_remove` / `queue_move`, `queue_rev`);
  "Copy to this PC" for a finished file; the run info card and per-point box
  only while the suite may submit. On hardware since 2026-10-06 (AFG + scope
  on the lab PC's server, watched from the office over 5551); phase 2 is
  simulation-tested only -- first real try: submit from the office with
  control, add a scan to a running queue, copy the file. Still open:
  * the watching suite's Scan tab builds against the lab's instruments only
    when the server reports them (`instruments` in its status, i.e. a
    server that follows the launcher); a server started with a FIXED
    registry (`--sim`) exposes no parameter list, so the watcher's Scan tab
    shows its own simulator and only the server's verdict at submit is the
    lab's. A `get_registry` verb (the server's parameter manifest) would
    close that gap;
  * inserting at a chosen place from the GUI: "+ Add to queue" (and a
    queue loaded with Load scan... while the server is busy) appends; Up /
    Down then place it. The verb takes an `index` already;
  * the lab PC's own suite (run on its server) still has no queue card --
    queue edits there go through a watcher view or a script.

- **Run catalogue -> ELN upload**: the catalogue is done (2026-10-04: the
  suite's Catalogue tab, `scan_core/catalogue.py`, `python -m
  scan_core.catalogue`; see the scan-core README and developer notes 4c).
  Still open: uploading a run (its header + a preview figure, optionally the
  file) to an electronic lab notebook -- which ELN, and whether the upload
  happens automatically after each scan or from the Catalogue's context menu.
  Smaller follow-ups: a `where` over AXIS ranges ("field covers 50"), `or`
  in `where`, and a scan-status flag in the file (aborted / complete) so the
  catalogue can show it.

- **Repeat / time in scans**: DONE 2026-10-04 (branch `repeat-axis`) -- the
  `repeat` axis (keep every repeat or store the average, optional
  `interval_s` for a time series; scan-core README, "Repeating and
  averaging"). Still open: a real TIME axis / a per-point timestamp recorded
  with every point (today only the interval pacing exists, the time each
  point was measured is not stored). (`average` with a fly axis: DONE
  2026-10-09, pixels pooled over the repeats. Collapsing the flown axis into
  one mean per row: DONE 2026-10-10, `collapse: mean` on the fly axis.)

- **Encryption (CurveZMQ)**: in every module since 2026-10-04; the lab runs
  `warn` for every module (`"*"`) since 2026-10-04 -- the week of `warn` is
  over. Next: `enforce` (Lukas's decision) -- before that, make the keyring
  folder writable only by the lab's admin. The installer does not ship
  `tools/` yet, so an installed PC has no `tools/keys.py`.
  `camera-control/scripts/kim_xy_calibration.py` still talks plain.
- **Control as an option** (off by default; encryption forces it on; one scan
  at a time per instrument stays always on): agreed, not built yet.
- **Instruments on this PC**: vendor probes are in (kim, camera, usb6001,
  signalhound, hf2, pm16, pm400). Not yet run on the lab PC: hf2 and pm400
  (no environment there); open: how LabOne lists an HF2, the PM400's USB ids.
- **AaltoView updates**: `scan-core/uv.lock` pins one AaltoView commit; a new
  viewer reaches the lab only after `uv lock --upgrade-package aaltoview` in
  scan-core is committed. A GitHub Action in AaltoView that opens that pull
  request automatically is planned (needs a token).

## Deliberately not built

- **Routine control flow** (2026-10-04). The five generic routine steps are
  DONE -- `wait_until`, `abort_if`, `skip_if`, `pause`, `comment`,
  `compute_set` (scan-core README, "Routines"; developer notes 4c). Lukas
  chose exactly these and asked to keep routines simple, so `repeat_until`
  (loops), `if` / `else` (branches) and `notify` (e-mail / chat messages)
  were deliberately NOT built: a routine is a list of steps run top to bottom,
  not a program. Work on several dies, or "measure until it converges", goes
  through a QUEUE of scans (one definition per die) or a script around
  `engine.run`. Revisit only with a concrete measurement that a queue cannot
  express.
