"""hp8648: HP / Agilent 8648D RF signal generator control (9 kHz - 4 GHz, GPIB).

A "set-and-forget" instrument: you command the RF output on/off, the CW
frequency and the output level, and it holds them. No control loop, but three
things make it more than a setter:

    spec         -- the instrument's frequency-dependent maximum level (it steps
                    down above 2500 MHz; option 1EA raises it), used both to
                    clamp and to publish a LIVE limit in `describe`.
    source       -- the brain (SignalSource): clamps, then lets one worker
                    thread own the GPIB bus, write in a safe order, wait for the
                    synthesiser to switch, read back and publish a snapshot. It
                    follows the reverse-power protection when it trips.
    backends     -- `base` (the interface), `sim` (a fake 8648D with resolution,
                    RPP and an error queue), `visa_8648` (the real box, SCPI
                    over GPIB, lazy pyvisa import).
    net          -- the ZeroMQ service + a brain-compatible client.

All modulation (AM / FM / PM / pulse) is switched OFF at connect: this module
drives a pure CW carrier. There is no phase control on the 8648.
"""

__version__ = "0.1.0"
