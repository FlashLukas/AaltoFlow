"""Networking: expose the HP 8648D brain as a ZeroMQ service, and a matching
client. REP for commands, PUB for status/events, on this module's own ports
(5619/5620, declared in module.toml) so it runs next to every other service.
"""
