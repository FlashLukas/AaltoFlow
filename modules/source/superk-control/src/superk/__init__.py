"""superk: NKT Photonics SuperK EXTREME supercontinuum laser + SuperK SELECT
acousto-optic tunable filter (AOTF) control.

The white-light laser (the EXTREME) makes a broad spectrum; the SELECT's AOTF
crystal, driven by an RF driver, diffracts up to 8 narrow lines out of it, each
line's wavelength set by its RF frequency and its strength by its RF amplitude.
One RF driver serves all the crystals, so exactly one crystal is active at a
time and the allowed wavelength range follows that choice.

    config       -- every tunable number as dataclasses, .ini save/load
    model        -- a plausible spectrum / AOTF model (sim + GUI drawing)
    backends     -- `base` interface, `sim` simulator, `nktp` the real laser
                    through NKT's NKTPDLL.dll (the only file touching it)
    laser        -- the brain `SuperK`: desired state, clamps, class 4 safety,
                    a worker thread that keeps the status snapshot fresh
    net          -- ZeroMQ service + client + describe manifest

A class 4 laser: emission is never switched on by starting anything, and is
switched off (with the RF) on shutdown.
"""

__version__ = "0.1.0"
