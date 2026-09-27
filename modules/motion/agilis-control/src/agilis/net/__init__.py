"""Networking layer: wire protocol, service, and client.

* :mod:`protocol` -- ports, topics, and (de)serialisation helpers.
* :class:`service.AgilisService` -- owns the brain, PUB status + REP commands.
* :class:`client.AgilisClient`   -- brain-compatible facade over the socket.
"""

from .client import AgilisClient, RemoteStatus  # noqa: F401
from .service import AgilisService  # noqa: F401
