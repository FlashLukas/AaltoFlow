"""Networking: expose the Synthesizer as a ZeroMQ service, and a matching client.

The suite's standard shape -- REP for commands, PUB for status/events -- on this
module's own ports (5591/5592) so it runs side by side with every other service.
"""
