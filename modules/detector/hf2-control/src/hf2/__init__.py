"""hf2: Zurich Instruments HF2LI lock-in amplifier control.

The HF2LI is the 50 MHz, two-signal-input Zurich lock-in. This package uses two
of its demodulators as two measurement CHANNELS (by default ch1 = signal input
1, ch2 = signal input 2) and also reads the two auxiliary inputs.

What you control per channel:
    reference     internal (our oscillator, frequency set from here) or
                  external (a PLL locks the oscillator to a reference signal;
                  the frequency is then MEASURED, not set)
    frequency     only in internal mode
    time constant and filter order of the demodulator's low-pass filter

What you read:
    X, Y, R, theta and the demodulation frequency per channel, and AUX IN 1/2.

Layers, same as every module in the suite:

    config       -- every tunable number as dataclasses, INI save/load
    backends     -- `base` = the interface, `sim` = a simulated HF2LI with real
                    filter dynamics, `zhinst_hf2` = the real instrument through
                    the LabOne API (zhinst-core)
    lockin       -- the brain: clamps, pushes settings, runs ONE polling thread
                    that owns every hardware read, and does settle-aware
                    acquisitions (trigger -> wait N time constants -> latch)
    net          -- ZeroMQ service + client on ports 5569/5570

A lock-in is a set-and-forget instrument like the SMB100A, but it is mainly a
DETECTOR, and a detector with memory: after anything changes, the output needs
several time constants to settle. That is why the `acquire` verb exists -- see
`lockin.py`.
"""

__version__ = "0.1.0"
