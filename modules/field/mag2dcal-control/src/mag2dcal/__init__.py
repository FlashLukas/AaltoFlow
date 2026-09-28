"""mag2dcal: a 2-axis (vector) electromagnet on an NI DAQ, driven clMag style.

A SECOND, PARALLEL controller for the same magnet as `mag2d-control`, speaking
the same wire contract (same verbs, same status keys, same describe ids) so the
two are interchangeable for vna-control, scan-core and the launcher. What
differs is the control philosophy, borrowed from clMag-control:

    MEASURE the magnet once (field against drive voltage, per axis, and
    separately for each hysteresis leg), JUMP onto that curve from one side,
    TRIM the last couple of millitesla with a one-way PI, then FREEZE the
    output and leave it alone. A slow, deadbanded STABILIZER answers drift.

The freeze is the point. On a magnet whose hysteresis branch follows the
direction of travel, a controller that keeps nudging keeps dragging the field
across that branch and never settles; one that stops, does. See pid.py's
docstring for the mechanism and tests/test_freeze.py for the measurement.

Hardware provenance: the LabVIEW program "RotSampleInVNA" -- two coil drives
(AO), two Hall probes and two thermometers (AI), a cooling-water flow switch
(DI) and an output-enable line (DO). Details in ..\\mag2d-control\\README.md.

Package layout:
    config       -- every tunable number as dataclasses, saved to / loaded from .ini
    calibration  -- the measured B(V) curves, the sweep plan, save / load
    pid          -- one axis of the seek: jump, settle, one-way trim, freeze
    controller   -- the brain: setpoint model, state machine, interlocks, the loop
    sim_system   -- build a controller on the simulated magnet
    backends     -- base (the Protocol), sim (the simulated magnet), nidaq (real)
    net          -- ZeroMQ service, client, protocol, describe
    apps         -- the GUI and the calibration viewer
"""

__version__ = "0.1.0"
