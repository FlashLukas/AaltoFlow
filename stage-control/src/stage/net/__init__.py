"""Networking layer: wire protocol, service, and client.

* :mod:`protocol` -- ports, topics, and (de)serialisation helpers.
* :class:`service.StageService` -- owns the brain, PUB status + REP commands.
* :class:`client.StageClient`   -- brain-compatible facade over the socket.
"""

from .client import RemoteStatus, StageClient  # noqa: F401
from .service import StageService  # noqa: F401
