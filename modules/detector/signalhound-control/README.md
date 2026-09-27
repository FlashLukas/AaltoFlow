# signalhound-control -- Signal Hound SA44B / SA124B spectrum analyser (+ USB-TG44A)

A swept **spectrum analyser** for the suite: power-per-bin traces in dBm, and --
with the **USB-TG44A tracking generator** on -- the **transmission** of whatever
sits between TG and analyser, measured against a stored **thru**:

    T(f) [dB] = P_dut(f) - P_thru(f)

One module drives whichever analyser is plugged in: the **SA44B** (1 Hz -
4.4 GHz, RBW up to 250 kHz) or the **SA124B** (100 kHz - 12.4 GHz, up to 6 MHz);
the frequency envelope follows the model it finds. It talks to Signal Hound's
**sa_api.dll** through ctypes (`--real`), or simulates one: a signal generator
with harmonics on a realistic noise floor, and a band-pass filter behind the
tracking generator.

![front panel](../../../front-panels/signalhound.png)

*(simulator: an SA44B, 1 GHz +- 100 MHz at 100 kHz RBW, the generator's tone at
1.005 GHz / -35 dBm on a -91 dBm floor.)*

Ports **5587 / 5588**.

> **The real backend has not run on an analyser yet.** Every call into
> sa_api.dll that still needs checking on the hardware is marked `# VERIFY` in
> `src/signalhound/backends/sa_api.py`.

## Run

```powershell
cd modules\detector\signalhound-control
uv sync --extra gui                              # there is no `real` extra: the DLL is loaded with ctypes
uv run scripts/run_service.py                    # simulated SA44B + TG44A
uv run scripts/run_service.py --model SA124B     # simulate the 12.4 GHz one
uv run scripts/run_service.py --real             # the first Signal Hound found
uv run scripts/run_service.py --real --serial 12345678 --model SA124B
uv run scripts/run_gui.py --connect localhost
uv run scripts/signalhound_console.py acquire
```

For `--real`, install Signal Hound's **Spike** software (it brings the USB
driver and `sa_api.dll`), make sure Spike itself is **closed** (it would hold
the device), and either put the DLL on the PATH or name it with `--dll` /
`hardware.dll_path`. The launcher only passes `--real`, so a PC keeps its choice
in `signalhound-control\signalhound.ini` (not in git), read by `run_service.py`:

```ini
[hardware]
model = SA124B
serial = 12345678
dll_path = C:\Program Files\Signal Hound\Spike\sa_api.dll
```

`model = auto` accepts whatever is plugged in; a named model makes the service
**refuse** a different one, so a swapped USB cable is noticed.

## Start-up changes nothing

Starting the service (or a GUI with its own analyser) only READS the analyser:
model (which sets the frequency and RBW envelope), serial, API version and
whether a USB-TG44A is paired. An SA44B/SA124B keeps no settings of its own
and has no call to read the host's settings back, so nothing else can be
adopted -- and nothing is written: no configure, no initiate, no abort. The
panel says "not configured yet" until a setting is changed, **Continuous** is
ticked or an acquisition is started; each of those configures the analyser
with the settings shown. `acquisition.sweep_on_start = true` in
`signalhound.ini` brings back sweeping at start. (The in-process simulator
GUI, `python -m signalhound.apps.gui`, sweeps at once: it has no instrument to
disturb.)

## The analyser picks its own bins

A Signal Hound decides how many frequency bins a sweep has -- from span and RBW
in spectrum mode, from the requested point count (within a factor 2) with the
TG on. The module therefore never assumes a grid: after every settings change it
reconfigures the analyser and uses the bins it reports (`points`, `bin_Hz` in
status; `get_frequencies` for a scan). Every trace carries `start_Hz`, `bin_Hz`
and `points`, and the frequency axis is rebuilt from them exactly.

## Settings

| control | verb | note |
|---|---|---|
| centre, span | `set_center`, `set_span` (or `set_start_stop`) | kept inside the model's range; with the TG on, inside 10 Hz - 4.4 GHz |
| reference level | `set_ref_level` | the API chooses gain / attenuation from it; too low = **OVERLOAD** |
| RBW | `set_rbw` | continuous to 100 kHz, then 250 kHz (and 6 MHz on the SA124B) -- snapped |
| VBW | `set_vbw` | at most the RBW (a narrower RBW drags it down) |
| detector | `set_detector` | `average` or `peak` (never misses a tone between coarse bins) |
| averages | `set_averages` | sweeps per acquisition, averaged in **power**, not in dB |
| tracking generator | `set_tg`, `set_tg_level`, `set_tg_points` | -30 ... -10 dBm; always **off** at start |

## The thru reference and transmission

1. Turn the tracking generator on (GUI TRACKING GENERATOR card, console `tg on`).
2. Replace the device under test by a thru and **Take thru** (verb
   `take_reference`, console `ref`): an acquisition like `acquire` whose result
   is kept as the reference.
3. Put the device back: every trace can now be shown as **Transmission vs thru**.

The TG's own output is not flat and the cables lose more at high frequency;
both cancel in the difference. Transmission is **refused** -- with a message
saying what differs -- with no thru, or one on another grid or TG level, or for
a spectrum-mode trace. Aborting a thru also clears the old one.

In a scan (scan-core), `take_reference` / `clear_reference` are **actions** a
routine can run, and the detectors are:

| detector | what |
|---|---|
| `signalhound.trace` | power per bin in dBm, dim `signalhound.freq` in GHz |
| `signalhound.transmission` | trace - thru in dB, same dim |
| `signalhound.peak_freq`, `.peak_level` | the highest bin of the averaged trace |
| `signalhound.noise_floor` | the median of the averaged trace |
| `signalhound.tx_center` | transmission at the centre frequency (TG mode) |
| `signalhound.overloaded` | the input compressed during the acquisition |

All from ONE acquisition per scan point (one acquire group). Changing span or
RBW between scan points changes the number of bins, and the scan engine refuses
a ragged trace -- scan the centre, the TG level or another instrument instead.

## The simulator

- **Spectrum mode:** the generator (`scene.tone_Hz`, `tone_dBm`) with 2nd and
  3rd harmonics, seen through a Gaussian RBW filter, with a phase-noise skirt.
  Noise floor = DANL (-150 dBm/Hz) + 10 log10(RBW), raised 1 dB per dB of
  reference level above -30 dBm (more input attenuation). The noise fluctuates
  like real noise; a narrow VBW smooths it without lowering it. Signals above
  ref + 5 dB compress and flag OVERLOAD. The SA44B cannot see beyond 4.4 GHz,
  the SA124B can.
- **Tracking mode:** TG level + TG flatness ripple (0.6 dB) + cable loss
  (sqrt f) + a 3rd-order Butterworth band-pass (1 GHz, 60 MHz, 1.5 dB loss)
  when `scene.dut_inserted`; switch it off for the thru.
- Every scene parameter is a live setting (`set_scene`, Settings > Scene), so
  "what does a narrower filter look like" is a scan axis.

## Tests

```powershell
uv run pytest -q          # offline; the real backend against a fake sa_api.dll
uv run scripts/smoke_test.py
python ../../../tools/check_modules.py signalhound --live
```
