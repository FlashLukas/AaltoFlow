# zpiezo-control

The **Z focus piezo** service of the AaltoFlow suite — a single-axis
Thorlabs KCube piezo, exposed over ZeroMQ so the camera module (and a coordinator)
can drive focus as an *external* instrument, just like the XY stage.

Set-and-forget family (like the RF generator): command a drive voltage, it holds
it, no control loop. Ports **5565** (commands) / **5566** (status).

## What it looks like

This is the one module with **no GUI** -- it is a headless service, and
mission-control deliberately does not open a window for it. So its front panel
is the console. A real session against the simulator:

```console
$ uv run scripts/run_service.py
z-piezo service: SIMULATOR
  commands tcp://0.0.0.0:5565   status tcp://0.0.0.0:5566
  Ctrl-C to stop.

$ uv run scripts/zpiezo_console.py status
{
  "ok": true,
  "status": {
    "connected": true,
    "voltage": 0.0,
    "target": 0.0,
    "v_min": 0.0,
    "v_max": 75.0
  }
}

$ uv run scripts/zpiezo_console.py set_voltage 7.5
{
  "ok": true,
  "voltage": 7.5
}

$ uv run scripts/zpiezo_console.py set_voltage 200
{
  "ok": true,
  "voltage": 75.0
}
```

That last one is the point of the Limits group: an out-of-range request is
**clamped, not refused**, and the reply tells you what actually happened. The
console speaks the raw protocol with `pyzmq` and `json` only -- it never imports
the `zpiezo` package, which is why it doubles as a check that the wire contract
really is the whole interface.

For the visible half of this service, see the **camera-control** panel: its
focus (Z) readout is being driven through here over ZeroMQ.

## Layout

```
zpiezo/
  config.py       Limits (v_min/v_max = safety envelope) + Hardware (serial, step)
  backends/       base.py (ZBackend Protocol); sim.py; kcube.py (real, lazy pylablib)
  zpiezo.py       the brain: set_voltage (clamped) / read_voltage / status
  net/            protocol.py (ports/helpers), service.py, client.py
scripts/          run_service.py, zpiezo_console.py, smoke_test.py
tests/            config, zpiezo, net
```

## Running (with `uv`)

```powershell
uv sync

# the service (simulator by default)
uv run scripts/run_service.py
# or the real KCube:
uv run scripts/run_service.py --real

# poke it by hand:
uv run scripts/zpiezo_console.py set_voltage 7.5
uv run scripts/zpiezo_console.py status

uv run scripts/smoke_test.py
uv run pytest -q
```

The camera module drives this service when its `hardware.use_remote_z = true`
(set `z_host` / `z_cmd_port` / `z_pub_port` in the camera config).

## Hardware pass (lab PC)

`pip install pylablib` (uncomment in `pyproject.toml`), install Thorlabs Kinesis,
set the KCube `serial` in config, and **verify the volt↔device-unit mapping** in
`backends/kcube.py` (some KPZ101 units command a fraction of full-scale, not volts).
Then run the service with `--real`.

## OneDrive `.venv` gotcha

If `uv sync` fails with *"Access is denied (os error 5)"*, put the venv off
OneDrive: `setx UV_PROJECT_ENVIRONMENT "%LOCALAPPDATA%\uv-venvs\zpiezo-control"`
(new shell), then `uv sync`.
