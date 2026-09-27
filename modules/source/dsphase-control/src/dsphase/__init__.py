"""dsphase: DS Instruments PS6000L wideband digital RF phase shifter control.

An ACTIVE phase shifter (I/Q modulator + amplifier + 30 dB output step
attenuator), 400-6000 MHz, -180..+180 deg in 0.5 deg steps, driven over a USB
virtual COM port with short SCPI-like ASCII lines. Set-and-forget: you command
a phase, an attenuation and RF on/off, and it holds them. The package:

    config       -- every tunable number as dataclasses, with .ini save/load.
    phasemath    -- rounding to the step, wrapping into -180..+180, and
                    reporting a readback in the caller's 360-degree branch.
    backends     -- `base` (the interface), `sim` (a fake PS6000L, the
                    default) and `ps6000l` (the real unit over pyserial).
    shifter      -- the brain: clamps, rounds, writes, and reads the unit back
                    on a worker thread.
    net          -- the ZeroMQ service and a matching client, so a GUI,
                    console or scan-core can drive it over localhost or the lab
                    Ethernet.
"""

__version__ = "0.1.0"
