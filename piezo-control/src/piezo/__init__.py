"""piezo -- control module for a 2D piezo stage (Jena d-Drive + PXY-200).

Part of the AaltoFlow instrument suite.  Same service/client shape as
``clMag-control``, ``smb-control`` and ``stage-control``: one service process
owns the stage (or its simulator) and publishes status + accepts JSON commands
over ZeroMQ; GUIs and a coordinator are clients.  This instrument is the fourth
in the suite, so it uses command port 5561 / status port 5562 (see
net/protocol.py).

Capabilities: move each axis to an absolute or relative XY position; switch each
axis between CLOSED-LOOP (strain-gauge servo, hysteresis-free, ~160 um travel)
and OPEN-LOOP (~200 um travel); control motion velocity via the controller's
native slew-rate limiter or a software ramp; read and set positions remotely; a
20-slot save/load position list; and a "zero here" relative frame.
"""

__version__ = "0.1.0"
