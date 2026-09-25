# scan-core — design & data model

The synchronization/coordinator module for the new AaltoFlow system (then called TRMOKE). It fixes the
three limits of the old LabVIEW `ControlTRMOKEV21setparam.vi`: the hard 2-loop
ceiling, the hardcoded parameter set, and the special-cased XY scan.

## One principle

**A scan is data, and every knob is a registered Parameter.** From those two
facts, arbitrary-N-D scanning, expandability, and an auto-built UI all follow.

## The locked data model: a *recipe*

A recipe fully describes a measurement with no hardware knowledge — a dict you
save as YAML, diff, version, share, and re-run headless (schema in
`schema/scan.schema.json`). Shape:

```yaml
name: field_freq_map
comment: 2-D FMR map
fixed: {rf_power: -5}          # set ONCE before sweeping (constant context)
axes:                          # ordered OUTER → INNER, no length limit
  - {type: linear, param: field,   start: 0,   stop: 120,  num: 41}
  - {type: linear, param: rf_freq, start: 800, stop: 2500, num: 81}
detectors: [lockin_r, lockin_phi]   # gettables recorded at every point
hooks:                              # actions bound to a level/cadence
  - {when: every_n_points, n: 200, action: autofocus}
output: {dir: ., basename: field_freq_map, format: netcdf}
```

### Axis types (`type` discriminates)

| type   | meaning                                             | → dims |
|--------|-----------------------------------------------------|--------|
| linear | `param` from/to with `num` (or `step`)              | 1 |
| array  | explicit `values: [...]`                            | 1 |
| file   | values loaded from a CSV/txt `path`                 | 1 |
| zip    | several params advanced in lockstep = **one** axis  | 1 |
| raster | `x`,`y` pair, `fast` axis = XY imaging               | **2** |

`Recipe.compile(registry)` turns the axis list into an ordered list of **Dim**s
(raster expands to two). Separating "how you describe a scan" (axes) from "how it
runs" (dims) is what lets XY imaging be *one* entry in the UI yet *two* genuine
dimensions in the data. This is the old *Define sweeps* + *Define looping* +
*Define XY scanning* tabs unified — with no 2-loop cap.

### Hooks

The old *Run loop* checkboxes ("AF after inner loop", "AF every n", "wait before
measure", "phase-lock at f change") become a small open registry of named
actions (`hooks.py`) bound to a moment: `before_scan`, `after_scan`,
`before_point`, `after_point`, `every_n_points`, `before_axis`, `after_axis`.
Add a capability = register one function; recipes can use it immediately.
The `call` action is a ROUTINE -- `args: {set: {param: value}, action: id}`,
blocking sets then one blocking registry Action -- see README "Routines".

## Registry (expandability)

`registry.py`: every knob is a `Parameter` with a stable `id`. `Settable` has
`limits` + a blocking `set` (clamp = the old *Limits* tab, enforced per-knob) and
a `get`; `Gettable` has `get`. A `Registry` is a namespace of them. The Scan
Builder populates its dropdowns from the registry and the engine drives/reads
through it — so **adding an instrument = adding Parameters, and it appears
everywhere with no other edits.** For real hardware a Settable/Gettable wraps one
of your ZeroMQ clients (the ~40-line adapters): `set` = client.set + wait-settled,
`get` = read status/detector. Shipped here: `build_sim_registry()` with a toy
MOKE/FMR physics (field-dependent resonance line + spatial spot) so scans show
real structure with no hardware.

## Engine

`engine.py` is an **odometer** over the compiled dims (dims[0] = slowest). At each
grid point it sets only the params of dims whose index changed (outer dims change
rarely), fires matching hooks, reads every detector, and stores values at the
multi-index → an `xarray.Dataset` with one named/units coordinate per dim and one
data variable per detector, plus full metadata (the recipe JSON, timestamps,
point count). Self-describing, arbitrary-N-D, netCDF-ready. 1-D, 2-D, 5-D are the
same code path.

## Old tab → new form

| Old LabVIEW tab            | New form |
|----------------------------|----------|
| Raw control                | auto-generated jog panel from the registry (next milestone) |
| Define sweeps / looping / XY | one Scan Builder axis stack → recipe.axes (N-D) |
| Scanning arrays            | `compile()` preview / summary |
| Limits                     | per-`Settable` clamp |
| Run loop                   | engine + hooks + ETA/progress |
| Scripting                  | the recipe YAML itself (save/load/version) |
| 1D / 2D / DATA plots        | slices of the one xarray cube |

## What's built vs next

Built: recipe schema + validate/compile, registry + sim, N-D engine, headless
demo (2-D/3-D/XY), and the Scan Builder GUI (palette, axis stack, summary/ETA,
run, live heatmap). Next: wrap the real ZeroMQ clients as Parameters (drop-in),
the auto-generated Raw panel, adaptive sampling, live fitting, and a "Coordinator"
card in Mission Control.
