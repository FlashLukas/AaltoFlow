"""tc200: a Thorlabs TC200 heater controller (one resistive heater, one PT100)
as an AaltoFlow module.

The TC200 runs its own PID loop in the box: you give it a setpoint in degC and
enable the output, and it heats. So this is a set-and-forget module whose one
subtle job is saying honestly when a setpoint has been REACHED (inside a
tolerance, continuously for a hold time), which is what a scan waits on:

    config     -- every tunable number as dataclasses, with plain-text save/load.
    backends   -- `base` defines the interface; `sim` is a heated block with a
                  PID so everything runs offline; `serial_tc200` drives the real
                  controller over its USB virtual COM port (the only file that
                  imports pyserial).
    heater     -- the brain: clamps setpoints, pushes them, polls, decides
                  `temperature_stable`, and keeps the heater safe.
    net        -- the ZeroMQ service + a matching client + `describe`.

Units are degC throughout: the TC200's serial interface speaks degC only,
whatever its front panel is set to display.
"""

__version__ = "0.1.0"
