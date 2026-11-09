"""Hardware backends for the KIM001 + PIA13 focus actuator.

* :class:`base.Kim001Backend`      -- the Protocol the brain talks to.
* :class:`sim.SimKim`           -- pure-Python simulator (default, no hardware).
* :class:`kinesis_kim001.KinesisKim` -- real Thorlabs KIM001 via pylablib (lazy).
"""

from .base import Kim001Backend  # noqa: F401
from .sim import SimKim  # noqa: F401
