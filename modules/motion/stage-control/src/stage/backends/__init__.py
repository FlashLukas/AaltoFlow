"""Hardware backends for the 3D stage.

* :class:`base.StageBackend`  -- the Protocol the brain talks to.
* :class:`sim.SimStage`       -- pure-Python simulator (default, no hardware).
* :class:`kinesis.KinesisStage` -- real Thorlabs BSC203 via pylablib (lazy).
"""

from .base import StageBackend  # noqa: F401
from .sim import SimStage  # noqa: F401
