"""scope: a two-channel oscilloscope -- the Siglent SDS1000CML+ series (the
lab's RS PRO RSDS1102CML+) over VISA, or a simulated MOKE bench.

    config     -- every tunable number as dataclasses, with .ini save/load.
    analysis   -- trace length, the zero-phase filter, per-channel numbers and
                  the hysteresis-loop numbers (numpy only).
    backends   -- `base` (the interface: generic, N channels, capabilities),
                  `sim` (the simulated bench), `siglent` (the real scope).
    scope      -- the brain: adopts the scope's settings, owns the trace
                  thread, the running average and the scan-safe `acquire`.
    net        -- the ZeroMQ service, client and `describe` manifest.
"""

__version__ = "0.1.0"
