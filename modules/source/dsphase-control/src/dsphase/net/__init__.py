"""Networking: expose the phase shifter as a ZeroMQ service, and a matching client.

The suite's shape -- REP for commands, PUB for status/events -- on ports
5589/5590 (declared in module.toml, overridable per PC in the launcher).
"""
