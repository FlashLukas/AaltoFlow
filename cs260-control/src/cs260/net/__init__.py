"""Networking: expose the monochromator as a ZeroMQ service, and a matching client.

REP for commands, PUB for status/events, on this module's own ports (5601/5602)
so it runs next to every other service of the suite.
"""
