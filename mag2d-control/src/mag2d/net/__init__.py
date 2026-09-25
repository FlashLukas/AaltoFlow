"""Networking: expose the vector magnet as a ZeroMQ service, and a matching client.

The service PUBlishes status (and events) and takes commands on a REP socket,
bound to 0.0.0.0 so localhost and the lab Ethernet are the same code. The client
SUBscribes to status and sends commands on a REQ socket, presenting a
Controller-compatible facade so the GUI can drive a remote magnet unchanged.
"""
