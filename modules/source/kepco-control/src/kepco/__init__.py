"""kepco: Kepco BOP 20-10 bipolar operational power supply control.

The BOP is a four-quadrant supply: it sources AND sinks, in both polarities,
up to +-20 V and +-10 A. It runs in one of two modes -- regulate the voltage
(with a current limit) or regulate the current (with a voltage limit).

This module is the supply ON ITS OWN: a plain, safe, scannable source of volts
or amps. (clMag-control also drives a Kepco, but as the actuator inside a
magnetic-field loop; nothing here knows about fields or calibrations.)

    config       -- every tunable number as dataclasses, with .ini save/load.
    backends     -- the thin hardware layer: `base` (the interface), `sim`
                    (a BOP driving a simulated coil), `bop_gpib` (the real
                    unit over GPIB, SCPI via pyvisa).
    supply       -- the brain: clamps to the safety envelope, runs the
                    software ramp (inductive loads!), ramps to zero before the
                    output goes off, measures V and I, latches acquisitions.
    net          -- the ZeroMQ service + a brain-compatible client.
    apps         -- the GUI, with the four-quadrant V-I indicator.
"""

__version__ = "0.1.0"
