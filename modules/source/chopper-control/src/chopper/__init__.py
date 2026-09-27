"""chopper: Thorlabs MC2000B optical chopper control.

A set-and-forget instrument with one thing that takes time: after a new
frequency the wheel must spin up and the PLL must lock. The package:

    config       -- every tunable number as dataclasses, with .ini save/load.
    blades       -- the blade table: slot counts, frequency range per ring, the
                    controller's blade / reference / output indices.
    backends     -- `base` (the interface), `sim` (a wheel with a first-order
                    spin-up), `mc2000b` (the real controller over USB serial).
    chopper      -- the brain: adopts the controller's state, clamps requests to
                    the blade's live range, polls the measured wheel frequency
                    and decides when it is LOCKED.
    net          -- the ZeroMQ service, its client facade and `describe`.
"""

__version__ = "0.1.0"
