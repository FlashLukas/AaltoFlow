"""Hardware backends for the DDR25 rotation stage.

* :class:`base.RotatorBackend` -- the Protocol the brain talks to.
* :class:`sim.SimRotator`      -- pure-Python simulator (default, no hardware).
* :class:`kinesis.KinesisRotator` -- the real K-Cube via pylablib (lazy import;
  deliberately NOT imported here, so the package loads without pylablib).
"""

from .base import RotatorBackend  # noqa: F401
from .sim import SimRotator  # noqa: F401
