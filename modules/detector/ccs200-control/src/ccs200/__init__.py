"""ccs200: the Thorlabs CCS200/M compact CCD spectrometer (200-1000 nm, 3648
pixels, USB), or a simulator of one.

    config        -- every tunable number as dataclasses, with .ini save/load.
    model         -- the simulator's physics: pixel -> nm, CCD response, a lamp
                     and Hg/Ar lines, offset + dark current + noise, saturation.
    backends      -- `base` (the interface), `sim` (the simulator), `tlccs`
                     (the real instrument through TLCCS_64.dll, loaded only
                     when opened); `real_backend(cfg)` builds it.
    spectrometer  -- the brain: clamps settings, owns the scan thread, the
                     scan-safe `acquire`, the dark spectrum and the analysis.
    net           -- the ZeroMQ service, client and `describe` manifest.
"""

__version__ = "0.1.0"
