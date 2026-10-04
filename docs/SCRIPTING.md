# Scripting AaltoFlow

The measurement suite runs one scan, or a queue of scans, exactly as they were
defined. Some experiments need more than that: *cool to 5 K, wait until the
temperature has been stable for ten minutes, focus, map; then the same at 10,
20 and 50 K*. For those there is a small Python API, `scan_core.api`:

```python
from scan_core import api

with api.connect() as lab:                  # the services Mission Control runs
    lab.set("clMag.field", 50)              # blocks until the field has settled
    lab.wait_until("ppms.temperature_stable", hold_s=600, timeout_s=7200)
    lab.run("camera.autofocus")             # waits until done; raises if it failed
    for t in [5, 10, 20, 50]:
        lab.set("ppms.temperature", t)
        ds = lab.scan("recipes/field_map.yaml", name=f"map_{t}K")   # saved like a GUI scan
    v = lab.get("hf2.r1")                   # one fresh reading
```

A script uses the same pieces as the suite: the parameters every module
describes, the settle rule each module declares, the scan engine, and the
suite's file naming. A recipe saved from the Scan tab runs in a script without
changes, and a file a script writes looks the same as one from the suite.

## Getting started

1. Start the instrument services in **Mission Control**, the same as before
   using the suite. A script connects to the services that are running. It
   does not start them.
2. Write your script. A good place for it is a folder of your own; the
   examples are in `scan-core/examples/`.
3. Run it **from the `scan-core` folder** with `uv`. That way it uses
   scan-core's Python environment, where `scan_core` is installed:

   ```powershell
   cd scan-core
   uv sync --extra gui                          # once, or after a pull
   uv run python examples/temperature_series.py
   uv run python C:\path\to\my_script.py        # a script kept anywhere else
   ```

   Relative paths inside the script (`"recipes/field_map.yaml"`) start in the
   folder you ran it from, `scan-core` here.
4. Try it on the simulator first: `api.connect(simulate=True)` needs no
   services and no hardware. The simulated parameters have no module prefix
   (`"field"`, `"rf_freq"`, `"lockin_r"`, `"pos_x"`, ...). The examples start
   with a `SIMULATE = True` switch and the parameter names for both cases.

To list what can be used, run `print(lab.summary())` in a script, or type the
same line in a Python prompt started with `uv run python`.

```
  settable  clMag.field                      Magnetic field   -95 .. 95 mT
  detector  hf2.r1                           R (ch 1)         V  (slow: acquires)
  action    camera.autofocus                 Find focus
```

### The examples

| script | what it shows |
|---|---|
| `examples/temperature_series.py` | a field x frequency map at several temperatures; each map waits until the temperature has stayed stable |
| `examples/wait_then_scan.py` | wait until a condition holds, focus, take a reference, run a recipe saved from the Scan tab |
| `examples/sample_positions.py` | the same scan on several dies: a list of positions, move, focus, one file per die |

As delivered, all three run on the simulator; `examples/sim_cryostat.py` adds a
simulated temperature for the first two. On the rig, set `SIMULATE = False`
and check the parameter names against `lab.summary()`.

## Reference

### `api.connect(...)` -> `Lab`

```python
api.connect(endpoints=None, host=None, include=None, simulate=False,
            data_dir=None, log=print, timeout_ms=3000, root=None, name=None)
```

| argument | meaning |
|---|---|
| *(none)* | connect to every module Mission Control knows about that answers, like the suite's "follow the launcher". Parameter ids are `<module>.<id>`: `clMag.field` |
| `include=["clMag", "hf2"]` | only these, and they must answer: if one does not, the connect fails and names it |
| `host="lab-pc"` | look for the local modules on another PC (remote services keep their own host) |
| `endpoints={"clMag": ("lab-pc", 5555, 5556)}` | exactly these services; the name becomes the prefix |
| `simulate=True` | the simulated instruments, with no hardware |
| `data_dir=...` | where `scan()` saves. The default is the folder chosen on the suite's Settings tab, otherwise `scan-core/out` |
| `log=print` | where messages go (time-stamped). `None` turns them off |
| `name="cooldown series"` | how the script shows on the instruments' control bars. The default is `script <file name>` |

Always use the `Lab` in a `with` block. However the script ends (it
finishes, raises an error, or you press Ctrl+C), the `with` block gives the
instruments back and closes the connections.

```python
with api.connect(include=["clMag", "hf2"]) as lab:
    ...
```

### `lab.set(pid, value, timeout_s=None)`

Sets a parameter and **blocks until it has settled**. "Settled" means
whatever the module declares in its `describe`: the magnet's `field_stable`,
a stage's echo of its target, a lock-in's settling time. A scan point waits
for the same thing. The call returns the value that was set.

```python
lab.set("clMag.field", 50)                 # returns when the field is stable
lab.set("kim.position_x", 120, timeout_s=300)   # a long move: allow more time
```

`set` refuses a value outside the parameter's limits and sends nothing.
The suite's Settable clamps to the limit; a script does not, because in a script a
typo (500 instead of 50) should stop the script, not drive the magnet to its
limit. If the instrument does not settle in time, `set` raises `SettleTimeout`.

### `lab.get(pid)`

Takes **one fresh reading**. A slow detector (a lock-in that has to settle, a
VNA sweep, a power meter that averages; in `describe` it has an
`acquire` block) is triggered and waited for, the same as at a scan point. Any
other detector is read directly. A detector that returns a whole trace
gives a numpy array.

```python
r = lab.get("hf2.r1")          # triggers an acquisition, waits for it, reads
trace = lab.get("vna.s")       # a complex array, one value per frequency
```

### `lab.read(pid)`

Returns the **current** value from the status stream. It starts no
acquisition and claims no instrument. Use it to watch a value. For a slow
detector, `read` returns its *last* acquisition, so use `get` when you
measure.

```python
lab.log(f"T = {lab.read('ppms.temperature'):.2f} K")
```

### `lab.run(action_id, **args)`

Runs an action (autofocus, take a reference, ...) and **waits until it has
finished**. If the action failed or did not finish in time, `run` raises
`ActionFailed`. A module that reports an outcome is checked: a camera autofocus
that found no peak counts as a failure. Keyword arguments replace the action's
declared defaults.

```python
lab.run("camera.autofocus")
try:
    lab.run("camera.autofocus")
except api.ActionFailed as exc:
    lab.log(f"no focus ({exc}); measuring anyway")
```

A script can only run an action whose module says how to tell that it has
finished (a `wait` block in its `describe`). The scan routines have the same
rule.

### `lab.wait_until(condition, hold_s=0, timeout_s=3600, poll_s=0.5)`

Blocks until `condition` is true **and has stayed true for `hold_s`
seconds**. If it becomes false during the hold, the hold starts again. After
`timeout_s` it raises `WaitTimeout` (`timeout_s=None` waits forever).
It returns the number of seconds waited.

The condition can be written as a string:

| condition | true when |
|---|---|
| `"ppms.temperature_stable"` | the value is true |
| `"not clMag.locked"` | the value is false |
| `"ppms.temperature < 5.05"` | the comparison holds (`<  <=  >  >=  ==  !=`) |

or as a function with no arguments:

```python
lab.wait_until("ppms.temperature_stable", hold_s=600, timeout_s=4 * 3600)
lab.wait_until(lambda: abs(lab.read("ppms.temperature") - 5) < 0.02, hold_s=300)
```

A misspelt parameter name fails immediately, not an hour into the wait.
`wait_until` only reads values, so it claims no instrument.

### `lab.scan(recipe, name=None, comment=None, save=True, data_dir=None, **meta)`

Runs one scan and **saves it the way the suite does**: the file is
`<data dir>/<YYYY-MM-DD>/<HHMMSS>_<name>.nc`. A scan longer than 100 points is
also saved at checkpoints (every tenth). The method returns the xarray
`Dataset`.

- `recipe` can be a path to a `.yaml` recipe (saved from the Scan tab), a
  measured `.nc` file (its definition is reused), a `Recipe` object, or a dict.
- `name` and `comment` replace the recipe's own.
- Extra keyword arguments are stored in the file as attributes:
  `temperature_K=5`, `die="A1"`.
- `api.path_of(ds)` or `lab.last_path` gives the file name.
- `save=False` keeps the data in memory only.

```python
ds = lab.scan("recipes/field_map.yaml", name=f"map_{t}K", temperature_K=t)
lab.log(f"saved to {api.path_of(ds)}")
peak = float(ds["hf2.r1"].max())
```

Before anything moves, the recipe is checked against the instruments' current
limits (`RecipeInvalid` names the problems) and the data folder is checked for
writing (`CannotSave`).

**Ctrl+C during a scan aborts the scan cleanly.** The scan stops after the
point or settle wait it is in, the after-scan routine runs, the points measured
so far are saved, and then `KeyboardInterrupt` ends the script. A second
Ctrl+C stops at once. What was measured is still saved, but the after-scan
routine does not run.

`lab.scan_queue("my_queue.yaml")` runs a queue saved from the Scan tab one
scan after another and returns the datasets.

### `lab.parameters(kind=None)`, `lab.describe(pid)`, `lab.summary()`

`lab.parameters()` lists every id in sorted order. Pass `"settable"`,
`"detector"` or `"action"` to get one kind only. `lab.describe(pid)` returns
what an id is: kind, label, unit, limits, whether a detector is slow, and
the axes of an array detector. `lab.summary()` gives all of this as a table.

### `lab.log(text)`

Prints a time-stamped line through the same `log` as the API's own
messages. `lab.log_lines` keeps them all.

### `lab.release()`

Gives the claimed instruments back before the end of the `with` block. The
next command that changes one of them claims it again.

### Errors

Every error the API raises on purpose is an `api.ScriptError`:

| error | when |
|---|---|
| `ParameterNotFound` | no parameter, detector or action with that id; the message suggests close matches |
| `SettleTimeout` | a `set` or an acquisition did not settle in time |
| `WaitTimeout` | `wait_until` gave up; the message gives the last value |
| `ActionFailed` | an action failed or did not finish |
| `ControlRefused` | someone on another PC holds control of the instrument, or another scan uses it; nothing was sent |
| `RecipeInvalid` | the recipe names unknown parameters or sweeps outside the limits; nothing was moved |
| `CannotSave` | the data folder cannot be written (checked before the scan starts) |
| `NoInstruments` | `connect()` found no running service |

An instrument fault during a scan (a dead service, a failed hardware read, a
camera that lost its pattern) **stops** a scripted scan with `ScanFault`. The
measured points are saved first. The suite pauses for the operator instead, but a script
has nobody to wait for.

## Control and safety

A script follows the rules every client of an instrument follows
([README, "Control"](../README.md#control----one-controller-many-viewers)).
The rule for scripts is: **a script is treated exactly like a scan.**

- To an instrument, a script identifies itself the same way the scan engine
  does: as a "machine" client with the scan role. Before its first command
  that **changes** an instrument (`set`, `run`, `scan`, or `get` of a slow
  detector), the script **claims** that instrument, the same claim a scan
  makes.
- If someone on **another PC** holds control of that instrument, the claim is
  refused: the call raises `ControlRefused` naming them, and nothing was
  sent. Ask them to release control, or take control from a GUI on your PC
  first.
- If **another scan** uses the instrument (the suite, or a second script), the
  claim is refused the same way. Only one scan engine drives an instrument at a
  time.
- If **nobody** holds control, your PC gets it while the script runs. A
  GUI on another PC is a viewer meanwhile. A GUI on **your own** PC does not
  block the script, because control belongs to a PC.
- The claim is kept **until the `with` block ends** (or `lab.release()`), not
  just for one call. A script is one long experiment, and another scan must not
  start driving the magnet between two of its steps. Heartbeats keep the claim
  alive through hours of waiting. If the script crashes, its instruments are
  free again after 10 s.
- Reading does not need control: `read`, `wait_until`, `parameters` and
  `describe` never claim anything.
- An old service that does not know about claims cannot be protected. The
  script says so in its log and carries on, the same as a scan does.

Why this choice: a bare "machine" client bypasses the control lock, which is
right for the camera moving the stage during an autofocus but wrong for a
script someone runs while a colleague on another PC is aligning. With a scan's
claim, the script can never change an instrument behind the back of the person
who holds control.

## How it relates to the suite and the queue

- **A recipe is shared**: a scan defined and saved in the Scan tab, or the `.nc`
  of a scan done there, is what `lab.scan(...)` runs. Build the scan in the GUI,
  check it there, then loop over it in a script.
- **The files are shared**: same folder (the Settings tab's data directory),
  same names, same contents. The Data tab and AaltoView open them, and the
  file's attributes record that a script wrote it (`saved_by`, `script`).
- **The queue** in the Scan tab runs fixed scans one after another. A script
  is the next step up, when something has to happen *between* the scans
  (a temperature, a wait, a move, a decision). `lab.scan_queue(...)` runs a
  saved queue from a script; Ctrl+C there stops the whole queue.
- **Do not run the suite's scan and a script on the same instruments at the
  same time**. You do not have to watch for this yourself: whichever comes
  second is refused (one scan per instrument). The suite can stay open to watch: its Control tab
  shows `scan 'script ...' running`.

## For developers

`scan-core/scan_core/api.py` is the whole API. It builds the registry with
`build_lab_registry(..., prefix=True)`, the same way the suite does, and
replaces `registry.scan_claim`, so the engine's per-scan claim goes through the
script's claims, which last until the end of the `with` block. The file naming
and the atomic write are in `scan_core/autosave.py`, shared with
`apps/scan_builder.py`. Tests: `scan-core/tests/test_api.py` (simulator, the
fake service with the real control gate, and the three examples).
