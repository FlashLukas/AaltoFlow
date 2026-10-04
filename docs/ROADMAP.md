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

### Camera: autofocus at a fixed AF position, then back (2026-10-02)

Sometimes focus must be found somewhere else than where you measure (a
feature with contrast, a clean area): measure, go to the AF position, find
focus, come back, carry on. As one camera ACTION a scan can run (Before /
After / Throughout, e.g. "start of each sweep of Scan point X").

- New action `autofocus_at_position` ("Find focus at AF position"), next to
  `autofocus` in describe, with the same `wait` block (target_key af_id,
  af_running false, af_error OK) so a scan waits for the whole round trip.
- The AF position is camera config (saved with the pattern / ini), either
  - an array INDEX (ix, iy) -- moves with the pattern, or
  - a position in um relative to the main template (as `set_laser_target`),
  plus a button "Set AF position here" in the camera GUI and a marker on the
  image (a distinct colour, labelled AF).
- The brain: remember the current target (selected index or laser target),
  go to the AF position and wait until stable (the stabiliser / laser
  target loop, as a scan axis does), run the configured autofocus, go BACK to
  the remembered target and wait until stable again, then report done. Z
  stays where the autofocus put it (the point of the exercise: same focus
  plane, assuming the sample is flat between the two places; a tilt
  correction could come later).
- Failure: if the move or the autofocus fails, still return to the measuring
  point, then report the error (af_error) so the scan's wait fails clearly;
  Kill AF (safety verb) aborts the whole trip and leaves the stage where it
  is.
- Needs tracking and a calibrated spot, like the stabiliser; says so if not.

Effort: ~1 day with tests on the camera simulator (round trip ends on the
original point; Z changed; a failing autofocus still returns; Kill AF).

### Oscilloscope module (first: Digilent Analog Discovery 3) for MOKE hysteresis loops (2026-10-03, spec agreed)

**Purpose.** Classical laser MOKE hysteresis loops: the magnet is driven
continuously (~30 Hz); one scope channel measures the field (Hall probe) or
the magnet current, the other the light intensity; the loop is channel 2
against channel 1 (XY). A scan point = one averaged loop, so loops can be
recorded against anything else in a scan (position on the sample via the
camera's scan points -> spatially resolved loops, temperature, angle, ...).

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

**Open:** which instrument gets W2's sync (its trigger input level); running
average as the mean of the last N traces or an exponential one (N = time
constant); whether filtering should also apply to the live display only.

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

The microscope camera (mono, 1936 x 1096) as a detector: one frame per
point, or a single snapshot. The engine and the file already handle 2D
detectors; the camera backends already keep a full-depth 16-bit copy of each
frame (`last_deep()`).

- Phase 1 (~2-3 days): 16-bit storage with a measured mask (above); a
  camera command and describe entry for the deep frame, with an acquire step
  so each point gets a new frame; the frame as a binary part of the reply,
  not base64 JSON; compression; a live image view; a 12-bit simulator mode.
  Auto exposure / gain off during a scan.
- Phase 2 (~2 days): write each frame to the file as it arrives, for
  full-frame maps on large sensors (a 12 MP 16-bit 30 x 30 map is 22 GB).
  Cropping a region around the laser spot is the cheaper alternative.

## Open work

- **Encryption (CurveZMQ)**: in every module since 2026-10-04; the lab runs
  `warn` for kim + camera. Next: widen the lab policy to `"*"` (restart the
  services), a week of `warn`, then `enforce` -- before that, make the keyring
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
