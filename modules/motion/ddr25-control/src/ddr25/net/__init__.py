"""Networking layer: wire protocol, service, and client.

* :mod:`protocol` -- ports, topics, and (de)serialisation helpers.
* :class:`service.Ddr25Service` -- owns the brain, PUB status + REP commands.
* :class:`client.Ddr25Client`   -- brain-compatible facade over the socket.
"""

from .client import Ddr25Client, RemoteStatus  # noqa: F401
from .service import Ddr25Service  # noqa: F401
