"""Hardware backends for the SmarAct linear positioner.

* :class:`base.SmaractBackend` -- the Protocol the brain talks to.
* :class:`sim.SimScu`          -- pure-Python simulator (default, no hardware).
* :class:`scu.ScuStage`        -- real SmarAct SCU via its DLL (ctypes, lazy).
"""

from .base import SmaractBackend  # noqa: F401
from .sim import SimScu  # noqa: F401
