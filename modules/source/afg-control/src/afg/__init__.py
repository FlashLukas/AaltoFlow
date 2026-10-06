"""afg: Tektronix AFG1062 two-channel arbitrary function generator (60 MHz).

Two outputs (CH1 / CH2): sine, square, pulse, ramp, noise, DC; frequency,
amplitude, offset, phase, pulse duty and ramp symmetry per channel, plus the
"CH2 follows CH1" coupling (a synchronous trigger next to a drive signal).
USB-TMC, SCPI.

    config     -- every tunable number as dataclasses, with .ini save/load.
    waveforms  -- the waveform arithmetic (shapes, peak voltage, load factor).
    backends   -- `base` defines the interface (generic, N channels); `sim` is
                  a fake AFG1062 so everything runs offline; `tek_afg` drives
                  the real one (pyvisa, the extra "real").
    generator  -- the brain: desired state per channel, safety clamps, one
                  worker thread that owns the instrument and reads it back.
    net        -- the ZeroMQ service, a matching client, and `describe`.

It speaks the suite's wire contract (docs/DEVELOPER_NOTES.md section 4), so
scan-core can sweep `afg.ch1_amplitude` without knowing anything about it.
"""

__version__ = "0.1.0"
