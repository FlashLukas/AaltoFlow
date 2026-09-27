"""sr830: Stanford Research SR830 DSP lock-in amplifier control.

The SR830 is the classic 100 kHz single-phase-reference, dual-phase-output
lock-in: ONE demodulator giving X, Y, R and theta, four AUX IN and four AUX
OUT voltages on the rear panel, and a SINE OUT reference output. It talks GPIB.

What you control:
    reference     internal (its own oscillator, frequency set from here and
                  scannable) or external (it locks to REF IN; the frequency is
                  then MEASURED), harmonic, phase, SINE OUT amplitude
    input         A / A-B / current (1 Mohm or 100 Mohm), grounding, coupling,
                  line notch filters
    gain, filter  sensitivity (27 fixed ranges, 2 nV .. 1 V), dynamic reserve,
                  time constant (20 fixed steps, 10 us .. 30 ks), slope
                  (6..24 dB/oct), synchronous filter
    aux out       four voltages
    auto          Auto Gain, Auto Reserve, Auto Phase

What you read: X, Y, R, theta, the reference frequency, AUX IN 1..4, and the
overload / unlock status bits.

Layers, same as every module in the suite:

    tables       -- the SR830's discrete ranges (index <-> label <-> number)
    config       -- every tunable number as dataclasses, INI save/load
    backends     -- `base` = the interface, `sim` = a simulated SR830 with real
                    filter dynamics and overloads, `visa_sr830` = the real
                    instrument over GPIB (pyvisa)
    lockin       -- the brain (DspLockIn): clamps, pushes, reads back, ONE
                    polling thread that owns every reading, settle-aware
                    acquisitions (trigger -> wait N time constants -> latch)
    net          -- ZeroMQ service + client on ports 5599/5600
"""

__version__ = "0.1.0"
