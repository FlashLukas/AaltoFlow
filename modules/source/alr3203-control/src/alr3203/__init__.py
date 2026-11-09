"""alr3203: an ELC ALR3203 laboratory supply driving a magnet, made bipolar by
a polarity relay.

The ALR3203 is a single-output supply, 0..32 V / 0..6 A / 96 W, that regulates
voltage or current (whichever setpoint it reaches first). It only sources. A
relay in the magnet leads -- on a digital line of the setup's DAQ, run by
usb6001-control -- reverses the current, so setpoints here are signed. The
module ramps every change, flips the relay only at zero current, and switches
between voltage and current mode without a bump (built 2026-11-09 for a vacuum
chamber; it replaces the supply part of a LabVIEW VI -- the Hall probe is read
by usb6001-control, there is no field loop).

    config       -- every tunable number as dataclasses, with .ini save/load.
    backends     -- the thin hardware layer: `base` (the interfaces), `sim`
                    (the supply driving a simulated coil, a simulated relay),
                    `elc_serial` (the real supply over its USB COM port),
                    `remote_usb6001` (the relay on usb6001-control's line).
    supply       -- the brain: clamps, ramps, polarity, bumpless mode switch,
                    ramps to zero before the output goes off, measures V and I,
                    latches acquisitions.
    net          -- the ZeroMQ service + a brain-compatible client.
    apps         -- the GUI, with the V-I plane indicator.
"""

__version__ = "0.1.0"
