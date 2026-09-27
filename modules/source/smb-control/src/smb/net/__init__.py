"""Networking: expose the generator as a ZeroMQ service, and a matching client.

Identical shape to clMag.net -- REP for commands, PUB for status/events -- but on
DIFFERENT default ports (5557/5558 vs the magnet's 5555/5556) so both services
run side by side on one host and a coordinator can drive them together.
"""
