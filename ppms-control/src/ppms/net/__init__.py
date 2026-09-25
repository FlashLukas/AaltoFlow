"""Networking: expose the cryostat brain as a ZeroMQ service, a matching client,
and the `describe` manifest. REP for commands, PUB for status/events, on the
module's own ports (5579/5580) so it runs next to every other service.
"""
