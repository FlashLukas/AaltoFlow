"""windfreak: Windfreak Technologies SynthHD PRO v2 two-channel RF synthesizer.

Two independent outputs (RFoutA / RFoutB), 10 MHz - 24 GHz, up to +20 dBm,
per-channel phase, a shared internal or external reference, over a USB
virtual COM port with Windfreak's one-character command set.

    config       -- every tunable number as dataclasses, with .ini save/load.
    backends     -- `base` defines the interface; `sim` is a fake SynthHD so
                    everything runs offline; `synthhd` drives the real one
                    (pyserial, the extra "real").
    synthesizer  -- the brain: desired state per channel, clamps, one worker
                    thread that owns the hardware, and the status snapshot.
    net          -- the ZeroMQ service, a matching client, and `describe`.

It speaks the suite's wire contract (docs/DEVELOPER_NOTES.md section 4), so
scan-core can sweep `windfreak.a_frequency` without knowing anything about it.
"""

__version__ = "0.1.0"
