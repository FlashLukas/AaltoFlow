"""Networking layer: wire protocol, service, and client.

* :mod:`protocol` -- ports, topics, and (de)serialisation helpers.
* :mod:`describe` -- the self-description manifest (`describe` verb).
* :class:`service.ElliptecService` -- owns the brain, PUB status + REP commands.
* :class:`client.ElliptecClient`   -- brain-compatible facade over the socket.
"""

from .client import ElliptecClient, RemoteStatus  # noqa: F401
from .service import ElliptecService  # noqa: F401
