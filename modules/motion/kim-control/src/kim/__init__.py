"""kim -- control module for a 3D piezo-inertia stage (Thorlabs KIM101 + 3x PIA25).

Part of the AaltoFlow instrument suite.  Same service/client shape as
``clMag-control``, ``smb-control``, ``stage-control`` and ``piezo-control``: one
service process owns the stage (or its simulator) and publishes status +
accepts JSON commands over ZeroMQ; GUIs and a coordinator are clients.  This
instrument is the seventh in the suite, so it uses command port 5567 / status
port 5568 (see net/protocol.py).

What makes it different from its sibling ``stage-control``: the actuators are
piezo-inertia (slip-stick) PIA25 motors driven by a KIM101, whose native unit is
the STEP.  This module speaks two languages -- STEPS (steps, steps/s, steps/s^2,
drive volts) and MICROMETRES -- bridged by a per-axis ``um_per_step``
calibration.  You can command moves and velocities in either.
"""

__version__ = "0.1.0"
