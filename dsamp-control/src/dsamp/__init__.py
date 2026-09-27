"""dsamp: DS Instruments smart variable-gain RF amplifier (GB6000L family).

A small USB-powered wideband amplifier (10 MHz - 6 GHz) whose gain is set in
0.5 dB steps and whose output stage can be switched on and off, all over a USB
virtual COM port with SCPI-like text commands. Like the RF generator it is a
"set-and-forget" instrument: you command a gain, it holds it. What it adds is
DANGER DOWNSTREAM -- +30 dB into a mixer or a sample is how they die -- so the
package is built around a safety ceiling on the gain and an amplifier that is
always OFF at start and on the way out.

    config       -- every tunable number as dataclasses, saved to a .ini.
    model        -- the datasheet's typical gain-vs-frequency roll-off and a
                    soft compression curve: an ESTIMATE of what comes out.
    backends     -- the thin hardware layer. `base` is the interface; `sim`
                    a fake amplifier (runs anywhere); `dsi_serial` the real one
                    over pyserial.
    amplifier    -- the brain: clamps to the safety ceiling, quantises to the
                    device's gain step, owns the one poll thread that reads the
                    hardware, publishes status snapshots.
    net          -- the ZeroMQ service + a brain-compatible client.
"""

__version__ = "0.1.0"
