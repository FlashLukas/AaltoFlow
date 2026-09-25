# AaltoFlow · Mission Control

The launcher. It **finds** the suite's modules, starts their services, opens
their GUIs, and shows what each one can do.

![mission control](../front-panels/mission-control.png)

## Where the module list comes from

Nothing is listed by hand. Every folder next to this one that contains a
**`module.toml`** is a module (contract: `../INSTRUMENT_MODULE_GUIDE.md`
section 11). Its card shows the **name, description and icon** from that file.
Drop a new module folder in and its card appears within a few seconds (or press
**Rescan**); delete it and the card goes.

The **variables** (controls and measured values) come from the running service
itself, through `describe`. Press **Variables** on a card to see them. They are
remembered after the service stops, marked "last seen".

## Per module

- **Service / Stop**: start or stop the service. **real** runs it on the
  instrument (`--real`); unticked is the simulator.
- **GUI**: opens the front panel, connected to the live service when it is up.
- **Ports**: change the module's command/status ports on this PC. The service
  gets them on its next start; scan-core and other modules (the camera finds kim
  this way) follow. Clashes are refused.
- Status dot: green = running and started here, amber = up but started
  elsewhere (or a reachable remote service), grey = down.

## Services on other PCs

**Add remote…** asks for host and ports. **Test connection** asks the service to
`describe` itself, which tells the launcher what kind of module it is, so it
borrows the icon, description and GUI of the same module on this PC. You can
still add it while the other PC is off: pick the module type by hand. A remote
card has a GUI button but no Start/Stop, since the service belongs to that PC.
**Remove** deletes it from the list; the service itself is not touched.

## Where the choices are saved

Ports, real/sim flags and remote services go in `../suite_local.json`, which is
this PC's file (not in git). The measurement suite in scan-core reads the same
file, which is how it follows the launcher. Profiles are in `profiles.json`
next to this script.

## Profiles

A chip per named subset: click it and the launcher starts just those services, in
dependency order (each module's `start_after`, e.g. the camera after kim),
then opens their GUIs. **Full suite** is always there and means every local
module. **Exclusive** first stops launcher-started services that are not in
the profile. **Edit…** adds, renames and removes profiles. A member that is not
found on this PC today is kept and skipped.

## Run it

```bash
cd mission-control
uv sync
uv run python mission_control.py
uv run pytest -q
```

Dependencies: PySide6, pyzmq (to ask services to describe themselves) and
`../suite-common` (discovery). It never imports an instrument package; it
launches their scripts.

## How things launch

- Services run with each project's own `.venv\Scripts\python.exe`, so the
  process the launcher tracks *is* the service and Stop kills it cleanly. With no
  `.venv` it falls back to `uv run`, and Stop also issues `taskkill /T`.
- Every started process gets `AALTOFLOW_ENDPOINTS` (where every module listens)
  and `PYTHONUNBUFFERED=1` (so its prints reach the log at once).
- Stop terminates, then force-kills after 2 s. A force-kill skips a service's
  clean shutdown; for a real magnet stop it from its own window.
