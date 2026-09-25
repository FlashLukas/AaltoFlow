"""clMag: magnetic field controller.

A Python re-implementation of a LabVIEW module that drives a Kepco BOP
power supply (constant-current mode, over GPIB) through an electromagnet,
and reads the resulting field from a Hall probe via an NI USB-6259 DAQ card.

Package layout:
    config       -- all tunable parameters as dataclasses, with plain-text
                    save/load so calibrations survive a restart.
    calibration  -- the measured B(I) curve: build it from a sweep, average
                    out hysteresis, interpolate, and save/load to a file.
    backends     -- the thin hardware layer. `base` defines the interfaces;
                    `sim` provides fake hardware so everything runs offline.
"""

__version__ = "0.1.0"
