"""pm400: Thorlabs PM400 optical power / energy meter console control.

    config    -- every tunable number as dataclasses, with .ini save/load.
    backends  -- `base` (the interface), `sim` (a simulated console with a
                 photodiode, a thermal and a pyroelectric head), `tlpmx` (the
                 real console through Thorlabs' TLPMX library, via ctypes).
    meter     -- the brain: follows the plugged-in head, clamps settings, owns
                 the polling thread and the scan-safe `acquire` (a mean of
                 readings that all started after the trigger).
    net       -- the ZeroMQ service, client and `describe` manifest.
"""

__version__ = "0.1.0"
