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
| [`camera`](../modules/imaging/camera-control) | IDS U3-38xCP (IDS peak) + KIM stage | **partly verified** (camera, features, spot, save; stabiliser not yet) | 2026-09-25 |
| [`signalhound`](../modules/detector/signalhound-control) | Signal Hound SA44B + USB-TG44A | **partly verified** (spectrum mode; TG measured through the raw API) | 2026-09-28 |
| scan-core | -- | real scans with pm16, kim + pm16 raster | 2026-09-25 |
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
- Not re-checked on the meter since: the hwlock claim (2026-09-27) and the
  fly-scan stream (2026-09-27).

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
  report "moving" on the first query after a move -- `# VERIFY` in the backend),
  and the fly-scan stream.

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
- Not yet checked on the rig: the stabiliser and laser placement at 63x, the
  autofocus routines on the real (hysteretic) Z, the lost-pattern fault of
  2026-09-28.

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
- Sweep time is ~0.05 s + span / 135 MHz/s; the first estimate was off by up to
  1000x.
- **The TG44A has no "off".** It keeps emitting its last frequency and level after
  an abort, a close and even the program exiting; only a new setting or unplugging
  changes it. The suite "switches it off" by PARKING it at 10 kHz, -30 dBm.
- Reading back the TG's frequency/level only echoes what the same program set --
  its state at start is unknown.
- A CW from the TG coexists with spectrum sweeps (level honoured: -30 dBm read
  -50.04 dBm after the 20.0 dB pad).
- In a TG sweep the level setting is IGNORED and the trace is transmission in dB
  relative to the TG output, not dBm; at most 1001 points, ~0.2 s + 1.3 ms/point.
- Not yet checked: `saStoreTgThru`, the TG's -10 dBm top end, the three-module
  split (spectrum analyser / signal generator / scalar network analyser) on the
  hardware.

## scan-core -- scans with real instruments

- 2026-09-15: wavelength scan with acquired pm16 detectors (see pm16).
- 2026-09-25: a 21 x 25 reflectivity map with kim moving and pm16 measuring; it
  showed the open-loop drift described under kim.
- Not yet on hardware: fly scans (first real try planned: kim + hf2, slowly), the
  pause-on-fault of 2026-09-28.

## Simulation only (no hardware pass yet)

agilis, ccs200, chopper, clMag, cs260, ddr25, dsamp, dsphase, dssg, elliptec,
gsp818, hf2, hp8648, k2450, kepco, ls455, mag2d, mag2dcal, piezo, pm400, ppms,
shsg, shsna, smaract, smb, sr7230, sr830, stage, superk, tc200, usb6001, vna,
windfreak, zpiezo. The per-module hardware checklists are in
[`DEVELOPER_NOTES.md`](DEVELOPER_NOTES.md) section 11 and each module's README.
