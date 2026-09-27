"""Networking: expose the heater brain as a ZeroMQ service, a matching client,
and the `describe` manifest. REP for commands, PUB for status/events, on the
module's own ports (5613/5614) so it runs next to every other service.
"""
