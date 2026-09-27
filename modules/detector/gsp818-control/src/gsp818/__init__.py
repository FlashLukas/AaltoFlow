"""gsp818: a swept spectrum analyser -- the GW Instek GSP-818 (9 kHz - 1.8 GHz)
with its tracking generator, or a simulator of one.

Two measurements: a SPECTRUM (power in dBm against frequency) and, with the
tracking generator on, a SCALAR NETWORK measurement (the trace relative to a
stored thru reference, in dB).

    config    -- every tunable number as dataclasses, with .ini save/load.
    model     -- the auto couplings (RBW/VBW/attenuation/sweep time) and the
                 simulator's physics: noise floor, detectors, carriers, DUT.
    backends  -- `base` (the interface), `sim` (the simulator), `gsp` (the
                 real instrument over pyvisa, imported only when opened).
    analyzer  -- the brain: clamps settings, owns the sweep thread, the
                 scan-safe `acquire` and the thru reference.
    net       -- the ZeroMQ service, client and `describe` manifest.
"""

__version__ = "0.1.0"
