"""shsg: the Signal Hound USB-TG44A tracking generator, used as a CW source.

One of three modules for the Signal Hound kit (Lukas, 2026-09-28):
    signalhound -- the spectrum analyser, and the ONLY process that opens the
                   USB devices (the analyser AND the tracking generator, because
                   the vendor API drives the TG through the analyser's handle);
    shsg        -- THIS module: the TG as a plain signal generator (CW on/off,
                   frequency, level), so a scan can use it like any RF source;
    shsna       -- a scalar network analyser (TG sweep + analyser).

So shsg never touches hardware. Its real backend is a CLIENT of the signalhound
service (backends/remote_sa.py, raw pyzmq); its simulator runs standalone.

    config       -- every tunable number as dataclasses, saved to an .ini.
    backends     -- base (the interface), sim (fake TG), remote_sa (the owner).
    generator    -- the brain: clamps, refuses while the TG is busy, reports the
                    APPLIED state.
    net          -- the ZeroMQ service + a client facade, describe manifest.
"""

__version__ = "0.3.0"
