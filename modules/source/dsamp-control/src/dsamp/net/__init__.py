"""Networking: expose the amplifier as a ZeroMQ service, and a matching client.

REP for commands, PUB for status/events -- the suite's wire contract -- on the
module's own ports (5593/5594) so it runs side by side with every other service.
"""
