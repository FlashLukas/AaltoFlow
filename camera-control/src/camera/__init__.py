"""camera -- vision brain + coordinator for the TRMOKE microscope.

Part of the AaltoFlow instrument suite.  Where the other modules each
control ONE instrument, this one is the *brain that ties them together*: it
reads a microscope camera, finds a fixed laser spot (threshold + centre of mass)
and a template pattern (pattern matching), pins a scanning-point array to the
template, and then closes two feedback loops by MOVING THE SAMPLE:

  * Stabiliser   -- every frame, null the spot -> selected-scan-point distance by
                    nudging the XY stage (drift compensation).
  * Autofocus    -- sweep Z, score focus (spot area / edge sharpness / FFT), and
                    move Z to the best level; optionally hold focus continuously.

It is a CLIENT of the piezo-control service for XY motion (ports 5561/5562) and
owns the Z focus piezo directly.  As the fifth instrument in the suite it serves
its own commands on port 5563 / status on 5564 (see net/protocol.py).  Same
service/client shape as every other module, so a coordinator can drive it too.

Templates (with metadata: spot-array distance, scanning-array details, pixel
size, objective) save/load as annotated PNG files.  Pixel size is calibrated per
microscope objective from a settings file.
"""

__version__ = "0.1.0"
