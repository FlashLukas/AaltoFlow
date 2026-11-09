"""kim001 -- control module for an objective-focus actuator (Thorlabs KIM001 + PIA13).

Part of the AaltoFlow instrument suite. One service process owns the KIM001
(single-channel K-Cube inertial piezo controller) or its simulator, publishes
status and accepts JSON commands over ZeroMQ (command port 5637 / status port
5638, see net/protocol.py); GUIs, the camera and scan-core are clients.

It is kim-control (KIM101 + 3x PIA25, the sample stage) cut down to ONE axis:
the PIA13 moves the microscope objective for focus and is published as axis
Z, with kim's verbs and 3-wide status lists, so the camera's existing kim Z
client (KimZFocus) drives it unchanged. X and Y are "not fitted" and refused.

The actuator is piezo-inertia (slip-stick): its native unit is the STEP. The
module speaks two languages -- STEPS and MICROMETRES -- bridged by the
``um_per_step`` calibration (per direction), and you can command moves and
velocities in either.
"""

__version__ = "0.1.0"
