"""Networking: expose the generator as a ZeroMQ service, and a matching client.

The suite's usual shape -- REP for commands, PUB for status/events -- on this
module's own ports (5625/5626). Not to be confused with backends/remote_sa.py,
which is the OTHER direction: this module as a client of the signalhound service.
"""
