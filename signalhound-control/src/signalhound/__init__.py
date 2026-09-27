"""signalhound: a Signal Hound SA44B / SA124B swept spectrum analyser with an
optional USB-TG44A tracking generator -- or a simulator of one.

    config       -- every tunable number as dataclasses, with .ini save/load.
    instruments  -- model ranges, allowed RBWs, the bin grid, sweep settings.
    physics      -- the simulator's world: a tone with harmonics on a noise
                    floor, and a band-pass filter behind the tracking generator.
    backends     -- `base` (the interface), `sim` (the simulator) and `sa_api`
                    (the real analyser through Signal Hound's sa_api.dll, loaded
                    only when opened).
    spectrum     -- the brain: clamps settings, owns the sweep thread, the
                    scan-safe `acquire` and the thru reference for transmission.
    net          -- the ZeroMQ service, client and `describe` manifest.
"""

__version__ = "0.1.0"
