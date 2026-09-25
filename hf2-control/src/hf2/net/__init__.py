"""Networking: expose the lock-in as a ZeroMQ service, and a matching client.

Same shape as every module in the suite -- REP for commands, PUB for status and
events -- on ports 5569/5570 (instrument #7 in the port scheme).
"""
