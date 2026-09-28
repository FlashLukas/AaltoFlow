# AaltoFlow -- instructions for Claude Code (and for any contributor)

AaltoFlow is lab-automation software: every instrument is a small standalone
**service** (ZeroMQ, JSON) with its own GUI, and a generic coordinator
(`scan-core`) runs N-dimensional scans over whatever services are running.
Developed by the NanoSpin group, Aalto University.

## Read before changing anything
1. `README.md` -- what the suite is and how to run it.
2. `INSTRUMENT_MODULE_GUIDE.md` -- **the blueprint for an instrument module**:
   folder layout, the brain / backends / net / apps split, the wire contract,
   `describe` (section 6b), GUI conventions, tests, and the step-by-step recipe
   for a new module (section 11).
3. `docs/DEVELOPER_NOTES.md` -- architecture, the wire contract in full, shared
   conventions, the light/dark theme mechanism, and **the numbered gotchas**
   (code comments cite them as "gotcha #N"). Check them before debugging
   anything similar.

Where the guide and the developer notes disagree, the developer notes win (they
are newer).

## Adding an instrument module
```
python tools/new_module.py <key> --like smb --category <category>   # or --like clMag / hf2
cd modules\<category>\<key>-control; uv sync --extra gui; uv run pytest -q
python tools/check_modules.py <key> --live           # the contract check
python tools/render_all.py <key>                     # offscreen front panel
```
The generator copies a working module, renames it and takes the next free port
pair. The launcher, scan-core and the tools discover the new module from its
`module.toml`; nothing else has to be registered. Modules live in
`modules/<category>/<key>-control` (category = the one in its module.toml);
after pulling the move on another checkout run `python tools/migrate_layout.py`
(developer notes, gotcha #36).

## Rules that are not negotiable
- **The wire contract** (developer notes, section 4): REQ/REP JSON commands,
  every reply `{"ok": true, ...}` or `{"ok": false, "error": ...}`; PUB/SUB
  telemetry; a reply means *accepted*, not *done*; universal verbs `status`,
  `info`, `get_config`, `set_config`, `describe`, `shutdown`.
- **`describe` tells the truth**: live limits, units, and a settle policy a
  scan can wait on. A detector that is slow declares an `acquire` block; an
  action a scan may run declares a `wait` block.
- **Simulation first**: every module runs without hardware. The real driver is
  the ONLY file that imports the vendor library, lazily inside `open()`, and
  every unverified hardware call is marked `# VERIFY`.
- **One physical address, one service**: every real backend claims its
  address with hwlock in `open()` (and releases it in `close()`; the simulator
  never claims). `src/<pkg>/hwlock.py` is a byte-identical copy of
  `suite-common/src/suite_common/hwlock.py` (gotcha #37).
- **Threads** (gotcha #1): status snapshots are rebuilt by a worker thread;
  setters change brain attributes, never the snapshot.
- **Theme**: never rebind `COLORS`; set the theme before building widgets.
- **Printed text is ASCII** (gotcha #14); open files with `encoding="utf-8"`.
- **Tests stay offline** and use non-default ports.

## Before you commit
- `uv run pytest -q` in every project you touched.
- `python tools/check_modules.py --live` if a service, a script or `describe`
  changed.
- Re-render the panels of any GUI you changed, in both themes.
- Explain the *why* in comments: the code is read by physicists who are not
  full-time programmers.

## Private notes
`CLAUDE.local.md` files (any folder) are personal working notes. They are
gitignored and loaded by Claude Code in addition to this file. Never commit
them, and never put names of lab PCs, user accounts, serial numbers or
personal e-mail addresses into tracked files.
