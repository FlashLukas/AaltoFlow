"""cs260: Newport (Oriel) Cornerstone 260 1/4 m monochromator control.

A motorised grating monochromator: it selects a narrow band of wavelengths out
of a broadband source. What can be commanded: the wavelength (THE scan axis),
which grating is in the beam, the built-in shutter, and -- if fitted -- the
filter wheel (order sorting) and the exit-port mirror.

    config         -- every tunable number as dataclasses, with .ini save/load.
    backends       -- `base` defines the interface; `sim` is a fake monochromator
                      whose moves take realistic time; `cornerstone` drives the
                      real one over GPIB (pyvisa, imported lazily).
    monochromator  -- the brain: clamps, sequences multi-step moves (grating
                      swap = shutter, grating, wavelength, shutter), and says
                      honestly when a move has ARRIVED.
    net            -- the ZeroMQ service + a matching client, plus `describe`.
"""

__version__ = "0.1.0"
