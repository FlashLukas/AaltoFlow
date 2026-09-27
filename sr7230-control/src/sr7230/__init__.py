"""sr7230: Signal Recovery (Ametek) Model 7230 DSP lock-in amplifier control.

The 7230 is a single-reference DSP lock-in (up to 120 kHz, or 250 kHz with the
7230/99 option) with an internal oscillator (OSC OUT), voltage and current
inputs, and rear-panel auxiliary ADC inputs. It speaks Signal Recovery's own
ASCII command set (TC, SEN, XY. ...), not SCPI, over Ethernet, USB or RS-232;
this package uses Ethernet.

What you control:
    reference     internal (our oscillator) or external (TTL / analog REF IN)
    oscillator    frequency and amplitude of OSC OUT, reference phase, harmonic
    signal        input (A, -B, A-B, current), coupling, full-scale sensitivity
    filter        time constant (1-2-5 table), slope 6..24 dB/oct, fast mode
    auto          auto-phase, auto-sensitivity, auto-measure

What you read:
    X, Y, R, theta (in V, or A in current mode), the reference frequency,
    overload flags, and ADC1 / ADC2.

Layers, same as every module in the suite:

    config       -- every tunable number as dataclasses, INI save/load
    tables       -- the instrument's discrete time constants and sensitivities
    backends     -- `base` = the interface, `sim` = a simulated 7230 with real
                    filter dynamics, `tcp7230` = the instrument over Ethernet
    lockin       -- the brain: clamps, snaps, pushes settings, runs ONE polling
                    thread that owns every hardware read, and does settle-aware
                    acquisitions (trigger -> wait N time constants -> latch)
    net          -- ZeroMQ service + client on ports 5621/5622

A lock-in is mainly a DETECTOR, and a detector with memory: after anything
changes, the output needs several time constants to settle. That is why the
`acquire` verb exists -- see `lockin.py`.
"""

__version__ = "0.1.0"
