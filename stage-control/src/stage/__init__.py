"""stage -- control module for a 3D coarse stage (Thorlabs BSC203, 3 steppers).

Part of the AaltoFlow instrument suite.  Same service/client shape as
``clMag-control`` and ``smb-control``: one service process owns the stage (or its
simulator) and publishes status + accepts JSON commands over ZeroMQ; GUIs and a
coordinator are clients.  This instrument is the third in the suite, so it uses
command port 5559 / status port 5560 (see net/protocol.py).

Capabilities: home each axis, set per-axis velocity/acceleration, jog/move,
a 20-slot save/load position list, per-axis user offsets, and a user-defined
2x2 XY coordinate transform.  Remote clients can read/set individual motor
positions and read/set the transform matrix.
"""

__version__ = "0.1.0"
