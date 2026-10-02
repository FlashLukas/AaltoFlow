# Roadmap -- parked ideas and open work

What has been discussed and deliberately left for later, with enough of the
reasoning that it can be picked up without the original conversation. Newest
first within each section. When an item is done, delete it here and describe
the result where it belongs (README, developer notes, guide).

## To discuss

### Data types: recording, storing and processing more than float64 (2026-10-02)

**Today** every detector is stored as a 64-bit float (8 bytes per value),
in memory (`engine.py`: `np.full(..., np.nan, dtype=float)`) and in the file
(`to_netcdf` with no encoding: uncompressed float64). Complex detectors are
complex128 in memory and two float64 variables (`<name>_real`, `<name>_imag`)
in the file. NaN means "not measured yet".

| type | recorded? | stored as |
|---|---|---|
| float | yes | float64 |
| complex (VNA) | yes | complex128 / two float64 variables |
| int, bool | yes | float64 (the registry knows the type; storage drops it) |
| text / enum (a state, a filter name) | no -- refused as a detector | -- |
| 1D / 2D arrays | yes | float64 / complex128 |

**Options, with a rough effort:**

1. Integers and booleans in their own type (int16, uint16, int32, ...):
   "not measured" becomes a netCDF `_FillValue` plus a measured mask instead
   of NaN. xarray turns the fill value back into NaN on reading, so AaltoView
   and analysis scripts most likely work unchanged (to be tested). Inside
   scan-core, window.py, the fly-scan binning and the live plots convert to
   float where they need NaN. ~1-1.5 days.
2. float32 as an opt-in per detector (half the size, ~7 significant digits).
   A few hours.
3. Lossless compression (zlib) for every variable in the file. A few hours.
4. Text and enums, CF-style: an integer code per point plus `flag_values` /
   `flag_meanings`, so it stays plottable and the viewer can label it; free
   text as netCDF-4 variable-length strings. AaltoView needs to show the
   names. ~1-2 days.
5. Ragged data (a different length at every point: peak lists, photon
   events): does not fit the scan grid; would need a separate table per point
   in the file. Hard -- only when an experiment needs it.

All of 1-4 together: ~3-4 days. Suggested order: 3 + 1 first (the biggest
saving, and the ground for camera images), then 4, then 5 only if needed.

**The storage type comes from `describe`, not from a setting.** Every
detector already declares `type` (float / int / bool / enum / string, enums
with `options`), so the engine can pick the storage itself:

| declared | stored as | "not measured" |
|---|---|---|
| bool | uint8 0/1 | 255 |
| enum | integer code + `flag_values` / `flag_meanings` | -1 |
| int | narrowest integer that fits `min`/`max` (or `bits`); int32 without bounds | the type's extreme value |
| float | float64 (float32 when the module says the precision allows) | NaN |
| string | text | "" |

- `min` / `max` are in the contract but almost only on controls (1 of ~445
  indicators declares them). Modules add them -- or a new optional
  `"bits": 12` for cameras and digitisers -- to the detectors where size
  matters. A backwards-compatible extension of `describe`.
- The bounds are the module's promise; the engine must never store a wrong
  value. A value that does not fit stops the scan with a clear message (as a
  changed array shape does today) -- no wrap-around, no clipping.
- With the type chosen automatically there is no per-detector setting and
  no UI for it: items 1 + 4 come to ~2-3 days, plus minutes per module for
  `bits` / `min` / `max`.

### Camera GUI: show which point an external client is moving to (2026-10-02)

When scan-core (or another machine client) drives the camera's scan point,
the window should make that obvious.

- Today the overlay already follows: `_refresh` copies the service's
  `selected_index_x/y` into the cfg mirror every tick, so the magenta aim
  marker moves to the point scan-core picks. But the Stabiliser's Index X / Y
  boxes are only filled when a pattern is loaded or Select is pressed, so
  they keep showing the old numbers while something else drives the point.
- To add: the Index X / Y boxes follow the service whenever the user is not
  editing them (no focus, no change since the last tick) -- the same rule
  the other live forms use; a line under them while a machine client drives
  it ("scan-core: point (3, 5) of 10 x 10, moving / stable"), using the
  control status's "also driving"; on the image, the target point drawn
  distinctly (and optionally the points already visited, and the path) while
  the stage moves; stable / distance readouts as now.
- View only, so it works in a viewer window too.

Effort: ~half a day with an offscreen GUI test (a machine client sets the
index; the boxes and the note follow; a box being edited is not
overwritten).

### Camera GUI: zoom into the live image freely (2026-10-02)

Today the camera window zooms only to the spot search region (the
"Zoom to spot region" button, and automatically during an autofocus). Wanted:
zoom anywhere, at any level, to look at details.

- The ground is there: `apps/camera_view.py` draws the picture, every
  overlay and every click through ONE transform (source rectangle + scale),
  via `set_zoom((x0, y0, x1, y1))`, so a click on a zoomed view already
  names the right image pixel.
- To add: mouse-wheel zoom about the cursor; pan with the middle button or
  Space + drag (the left button is taken: click-to-go, template ROI, scan
  rectangle); "Fit" and "1:1 pixels" buttons; the zoom level shown on the
  view; maybe a small overview inset with the zoomed rectangle.
- The autofocus zoom lies over the user's zoom and gives it back at the end
  (as the spot-region toggle does now).
- View only, so it stays usable in a viewer window (no control needed).

Effort: ~1 day with tests (zoom/pan maths, clicks on a zoomed and panned view
still hitting the right pixel, the autofocus hand-back).

### Oscilloscope traces, e.g. Digilent Analog Discovery (2026-10-02)

Record scope traces as a scan detector: one trace (or one per channel) at
every point, like a VNA trace or a spectrum.

- Fits the existing 1D-detector path: each channel is an array detector
  sharing a time axis (built from the sample rate and the trigger position,
  fetched once per scan like the VNA's frequency axis). An `acquire` block
  arms the trigger and waits until the capture is done (timeout from the
  record length and the trigger timeout); averaging N triggers in the module
  keeps the file small.
- Analog Discovery 2 / 3: two 14-bit scope channels (AD2 100 MS/s, AD3
  125 MS/s), a buffer of a few thousand to a few tens of thousands of
  samples per channel, plus a 2-channel waveform generator, digital I/O and
  small power supplies. Driven through Digilent's WaveForms SDK (the `dwf`
  library installed with WaveForms; Python via ctypes or a wrapper such as
  dwfpy) -- the real driver is the only file that imports it, lazily, as
  usual.
- Controls: timebase / sample rate, record length, vertical range and offset
  per channel, coupling, trigger source / level / slope / position,
  averages. The waveform generator (frequency, amplitude, offset, shape) can
  be controls of the same module and so scan axes. Safety verbs: stop the
  acquisition, generator output off, supplies off.
- One device, one service: claim the device's serial number with hwlock.
  "Instruments on this PC" does not list it yet (it is not VISA): a Digilent
  probe belongs with the other vendor-specific probes.
- Storage: a few thousand float64 per point is small. Raw 14-bit samples as
  int16 plus a scale factor would follow from "Data types" above.
- Later: a SCPI scope over VISA (Rigol, Keysight, Tektronix) as a second
  backend with the same describe, so scans and the viewer do not care which
  scope it is.

Effort: module with a simulator (sine / pulse with noise, trigger) and
tests ~3-4 days, plus a hardware session. **Needs first:** which Analog
Discovery (2 or 3), which channels and trigger, typical record length.

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

- **Control rollout** (PR #2, branch `control-rollout`): review and merge.
  `curve-security` and `instrument-discovery` both add an import on the same
  line of `mission_control.py`; when merging, keep both lines.
- **Encryption (CurveZMQ)**, branch `curve-security`: try it on the lab PCs
  in `warn` mode, then `enforce`; then roll it out to the other modules the
  way Control was. The installer does not ship `tools/` yet, so an installed
  PC has no `tools/keys.py`.
- **Instruments on this PC**: check against real GPIB and COM hardware on the
  lab PC; vendor-specific probes later (IDS camera, Zurich HF2, NI DAQ,
  Signal Hound, Thorlabs Kinesis).
- **AaltoView updates**: `scan-core/uv.lock` pins one AaltoView commit; a new
  viewer reaches the lab only after `uv lock --upgrade-package aaltoview` in
  scan-core is committed. A GitHub Action in AaltoView that opens that pull
  request automatically is planned (needs a token).
