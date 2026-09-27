"""agilis -- control module for a Newport Agilis 2-axis piezo stage (AG-UC2).

Part of the AaltoFlow instrument suite. One service process owns the AG-UC2
controller (or its simulator), publishes status and accepts JSON commands over
ZeroMQ (ports 5595 / 5596); GUIs and scan-core are clients.

Agilis actuators are slip-stick piezo motors with no encoder. The controller's
native unit is the STEP, and the size of a step depends on the step AMPLITUDE
(1..50) and on the direction. This module speaks STEPS and MICROMETRES, bridged
by a measured step size per axis and direction that remembers the amplitude it
was measured at.
"""

__version__ = "0.1.0"
