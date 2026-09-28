"""usb6001: a general-purpose NI USB-6001 DAQ as an AaltoFlow module.

    config       every tunable as dataclasses + .ini: per-channel AI/AO settings
                 and the direction (in / out / unused) of every digital line.
    backends     the hardware layer: `base` (the interface), `sim` (a fake card
                 so everything runs offline), `nidaq` (the real card, nidaqmx).
    daq          the brain: clamps and writes outputs, polls inputs, takes
                 FRESH readings for scans.
    net          the ZeroMQ service + client, and `describe`.
    apps         the GUI.
"""

__version__ = "0.1.0"
