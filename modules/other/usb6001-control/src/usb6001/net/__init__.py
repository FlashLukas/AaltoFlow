"""Networking: expose the DAQ brain as a ZeroMQ service, and a matching client.

Identical shape to clMag.net -- REP for commands, PUB for status/events -- but on
DIFFERENT default ports (5629/5630 vs the magnet's 5555/5556) so both services
run side by side on one host and a coordinator can drive them together.
"""
