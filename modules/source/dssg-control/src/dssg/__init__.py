"""dssg: DS Instruments SG12000L microwave signal generator control.

The SG12000L is a USB-powered 25 MHz - 12 GHz CW synthesiser that speaks SCPI
over a virtual COM port (USB-C) or, with the Ethernet option, over a TCP socket.
It is a "set-and-forget" instrument: you command RF on/off, frequency, power,
phase and the 10 MHz reference, and it holds them. The package layout is the
suite's standard one:

    config       -- every tunable number as dataclasses, with plain-text
                    save/load so a setup survives a restart.
    backends     -- the thin hardware layer. `base` defines the interface;
                    `sim` is a fake SG12000L so everything runs offline;
                    `dsi_scpi` drives the real unit over USB serial or TCP.
    synthesizer  -- the small "brain": holds the desired signal, clamps it to
                    the safety limits AND the unit's own range, pushes it to
                    the backend, and keeps a read-back status snapshot.
    net          -- expose the brain as a ZeroMQ service, plus a matching
                    client, so a GUI / console / scan-core can drive it over
                    localhost or the lab Ethernet.
"""

__version__ = "0.1.0"
