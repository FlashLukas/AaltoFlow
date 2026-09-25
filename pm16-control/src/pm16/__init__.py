"""pm16: Thorlabs PM16 USB optical power meter control.

    config    -- every tunable number as dataclasses, with .ini save/load.
    backends  -- `base` (the interface), `sim` (a simulated meter with a Si
                 responsivity curve), `tlpmx` (the real meter through Thorlabs'
                 TLPMX library, called with ctypes).
    meter     -- the brain: clamps settings, owns the polling thread, and the
                 scan-safe `acquire` (a mean of readings that all started after
                 the trigger).
    net       -- the ZeroMQ service, client and `describe` manifest.
"""

__version__ = "0.1.0"
