"""smaract -- control module for a SmarAct CLL42 linear positioner on an SCU.

Part of the AaltoFlow instrument suite. One service process owns the
positioner (or its simulator), publishes status and accepts JSON commands over
ZeroMQ; GUIs and scan-core are clients. Ports 5597 (commands) / 5598 (status),
declared in module.toml.

One closed-loop linear axis: absolute / relative moves in mm, a speed (the
controller's closed-loop step frequency), referencing on the distance-coded
encoder marks, stop, a "zero here" origin, 20 stored positions, and a
position stream for fly scans.
"""

__version__ = "0.1.0"
