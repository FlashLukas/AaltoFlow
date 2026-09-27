"""Hardware backends for the Agilis stage.

* :class:`base.AgilisBackend` -- the Protocol the brain talks to.
* :class:`sim.SimAgilis`      -- pure-Python simulator (default, no hardware).
* :class:`ag_uc2.AgUC2`       -- real AG-UC2 over its USB COM port (pyserial, lazy).
"""

from .base import AgilisBackend  # noqa: F401
from .sim import SimAgilis  # noqa: F401
