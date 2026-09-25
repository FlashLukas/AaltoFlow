"""zpiezo -- control module for a single-axis Z focus piezo (Thorlabs KCube).

Sixth instrument in the AaltoFlow suite and the simplest: a
**set-and-forget** axis (like the RF generator) -- you command a drive voltage
and it holds it; no control loop.  It exists so that the camera module can drive
focus (Z) as an *external* service over ZeroMQ, exactly the way it drives the XY
stage through piezo-control -- the coordinator end-goal where the vision brain
owns no motion hardware itself.

Ports: commands (REP) 5565, status (PUB) 5566.  Same wire protocol as every other
module, so a coordinator (or the camera) can treat it uniformly.
"""

__version__ = "0.1.0"
