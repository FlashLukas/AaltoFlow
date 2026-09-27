"""Networking: expose the supply as a ZeroMQ service, and a matching client.

The suite-wide shape -- REP for commands, PUB for status/events -- on this
module's own ports (5581/5582), so it runs next to every other service.
"""
