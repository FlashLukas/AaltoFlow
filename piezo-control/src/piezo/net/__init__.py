"""Networking layer: wire protocol, service, and client.

* :mod:`protocol` -- ports, topics, and (de)serialisation helpers.
* :class:`service.PiezoService` -- owns the brain, PUB status + REP commands.
* :class:`client.PiezoClient`   -- brain-compatible facade over the socket.
"""

from .client import PiezoClient, RemoteStatus  # noqa: F401
from .service import PiezoService  # noqa: F401
