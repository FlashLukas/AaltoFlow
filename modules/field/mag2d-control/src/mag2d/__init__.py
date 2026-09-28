"""mag2d: a 2-axis (vector) electromagnet on an NI DAQ.

Successor of the LabVIEW program "RotSampleInVNA": two coil drives (AO), two
Hall probes and two thermometers (AI), a cooling-water flow switch (DI) and an
output-enable line (DO). The field is held by a PI loop per axis, in calibrated
millitesla, running continuously while the output is energized.

Package layout:
    config       -- every tunable number as dataclasses, saved to / loaded from .ini
    pid          -- one axis of the loop: feed-forward + PI, clamp, slew, anti-windup
    controller   -- the brain: setpoint model, state machine, interlocks, the loop
    sim_system   -- build a controller on the simulated magnet
    backends     -- base (the Protocol), sim (the simulated magnet), nidaq (real)
    net          -- ZeroMQ service, client, protocol, describe
    apps         -- the GUI
"""

__version__ = "0.1.0"
