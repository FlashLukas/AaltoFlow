"""shsna: a SCALAR network analyser from the Signal Hound kit -- the USB-TG44A
tracking generator sweeps a tone through a device under test into the SA44B /
SA124B analyser, and |S21| in dB is that power over a stored THRU reference.

    config    -- every tunable number as dataclasses, with .ini save/load.
    physics   -- the simulator's chain (TG ripple, cable, 20 dB pad, band-pass
                 DUT, noise floor), power averaging, and the scalar summaries.
    backends  -- `base` (the interface), `sim` (standalone simulator) and
                 `remote_sa` (--real: a CLIENT of the signalhound service, the
                 only process allowed to hold the analyser and its TG).
    analyzer  -- the brain: clamps settings, owns the sweep thread, the
                 scan-safe `acquire` and the thru reference.
    net       -- the ZeroMQ service, client and `describe` manifest.
"""

__version__ = "0.1.0"
