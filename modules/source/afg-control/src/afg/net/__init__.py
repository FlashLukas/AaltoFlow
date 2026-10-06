"""Networking: expose the generator as a ZeroMQ service, and a matching client.

The suite's standard shape -- REP for commands, PUB for status/events -- on this
module's own ports (5631 / 5632, declared in module.toml), so it runs side by
side with every other service and a coordinator can drive them together.
"""
