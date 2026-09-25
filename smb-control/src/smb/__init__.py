"""smb: Rohde & Schwarz SMB100A RF signal generator control.

A sibling to `clMag` (the Kepco magnet controller). Where the magnet needs a
control loop -- ramp, PID, calibration, a state machine -- the SMB100A is a
"set-and-forget" instrument: you command RF on/off, output power, frequency and
phase, and it simply holds them. So this package deliberately drops all the
control machinery and keeps only the parts that carry over:

    config       -- every tunable number as dataclasses, with plain-text
                    save/load so a setup survives a restart.
    backends     -- the thin hardware layer. `base` defines the interface;
                    `sim` provides a fake generator so everything runs offline;
                    `visa_scpi` drives the real SMB100A over GPIB (SCPI).
    generator    -- the small "brain": holds the desired signal, clamps it to
                    the safety limits, pushes it to whichever backend is wired in.
    net          -- expose the generator as a ZeroMQ service, plus a matching
                    client, so a GUI / console / coordinator can drive it over
                    localhost or the lab Ethernet.

Because it speaks the same ZeroMQ shape as `clMag` (just on different default
ports), one coordinator process can sequence both instruments together:
"set field -> wait stable -> set RF -> measure".
"""

__version__ = "0.1.0"
