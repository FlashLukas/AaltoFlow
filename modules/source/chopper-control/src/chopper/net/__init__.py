"""Networking: expose the chopper as a ZeroMQ service, and a matching client.

REP for commands, PUB for status/events, on this module's own ports
(5609/5610) so it runs side by side with every other service on one host.
"""
