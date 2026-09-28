"""Networking layer: wire protocol, service, and client.

* :mod:`protocol` -- ports, topics, and (de)serialisation helpers.
* :class:`service.KimService` -- owns the brain, PUB status + REP commands.
* :class:`client.KimClient`   -- brain-compatible facade over the socket.
"""

from .client import KimClient, RemoteStatus  # noqa: F401
from .service import KimService  # noqa: F401
