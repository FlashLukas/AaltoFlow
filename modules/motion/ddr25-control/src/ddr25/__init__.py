"""ddr25 -- control module for a Thorlabs DDR25/M direct-drive rotation stage.

Part of the AaltoFlow instrument suite. One service process owns the stage (a
K-Cube brushless servo controller over USB, or its simulator), publishes status
and accepts JSON commands over ZeroMQ; GUIs, consoles and scan-core are
clients. Ports 5605 (commands) / 5606 (status), declared in module.toml.

One continuous rotary axis in degrees: absolute angle with a wrap policy
(literal / shortest / positive / negative), relative moves, velocity and
acceleration, homing (required before absolute moves), stop, a display zero,
stored orientations, and a position stream for fly scans.
"""

__version__ = "0.1.0"
