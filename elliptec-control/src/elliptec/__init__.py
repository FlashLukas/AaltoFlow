"""elliptec -- control module for Thorlabs Elliptec ELL14 rotation mounts.

Part of the AaltoFlow instrument suite.  One service process owns the serial
bus (or its simulator), publishes status and accepts JSON commands over ZeroMQ;
GUIs, consoles and scan-core are clients.  Ports 5607 (commands) / 5608
(status), declared in module.toml.

Every configured bus address (0-F) is one rotary axis: absolute and relative
moves in degrees, homing, speed in percent, stop, and a per-axis user zero.
"""

__version__ = "0.1.0"
