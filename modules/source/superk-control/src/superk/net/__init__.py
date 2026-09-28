"""Networking: expose the SuperK brain as a ZeroMQ service, and a matching client.

REQ/REP for commands, PUB/SUB for status and events, on this module's ports
(5611/5612, from module.toml) so it runs side by side with every other service.
"""
