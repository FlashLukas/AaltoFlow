"""Networking layer: wire protocol, service, and client.

* :mod:`protocol` -- ports, topics, and (de)serialisation helpers.
* :class:`service.SmaractService` -- owns the brain, PUB status + REP commands.
* :class:`client.SmaractClient`   -- brain-compatible facade over the socket.
"""

from .client import RemoteStatus, SmaractClient  # noqa: F401
from .service import SmaractService  # noqa: F401
