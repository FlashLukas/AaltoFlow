"""Networking layer: wire protocol, service, and client.

* :mod:`protocol` -- ports, topics, and (de)serialisation helpers.
* :class:`service.Kim001Service` -- owns the brain, PUB status + REP commands.
* :class:`client.Kim001Client`   -- brain-compatible facade over the socket.
"""

from .client import Kim001Client, RemoteStatus  # noqa: F401
from .service import Kim001Service  # noqa: F401
