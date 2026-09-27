"""k2450: Keithley 2450 SourceMeter (SMU) control.

An SMU sources a voltage or a current and measures the other one at the same
time, with a compliance limit so it never pushes the sample harder than you
allow. This package splits that the suite's usual way:

    config       -- every tunable number as dataclasses, with plain-text
                    save/load so a setup survives a restart.
    backends     -- the thin hardware layer. `base` defines the interface;
                    `sim` is a fake 2450 with a pretend sample (resistor,
                    diode, open) so everything runs offline; `scpi_2450`
                    drives the real instrument over VISA (SCPI).
    smu          -- the "brain" (SourceMeter): clamps every request to the
                    output envelope, keeps the output safe, and takes the
                    scan-safe readings (`acquire`).
    net          -- the ZeroMQ service + a matching client, so a GUI, console
                    or scan-core can drive it over localhost or the lab network.
"""

__version__ = "0.1.0"
