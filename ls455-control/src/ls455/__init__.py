"""ls455: Lake Shore Model 455 DSP gaussmeter (single-axis Hall probe) control.

    config    -- every tunable number as dataclasses, with .ini save/load.
    backends  -- `base` (the interface, unit conversion, probe ranges), `sim`
                 (a simulated 455 with drift, range-dependent noise and the DC
                 filter's lag), `ls455` (the real meter through pyvisa, GPIB or
                 RS-232).
    gaussmeter -- the brain: validates/clamps settings, owns the polling
                 thread, and the scan-safe `acquire` (mean +- sd of readings
                 that all started after the trigger and the filter's settling).
    net       -- the ZeroMQ service, client and `describe` manifest.

Every field value in this package is in millitesla (mT).
"""

__version__ = "0.1.0"
