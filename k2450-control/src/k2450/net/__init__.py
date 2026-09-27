"""Networking: expose the SMU as a ZeroMQ service, and a matching client.

REP for commands, PUB for status/events, on this module's own ports
(5623 / 5624) so it runs side by side with every other service.
"""
