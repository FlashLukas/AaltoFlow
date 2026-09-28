"""Measurement backends: the interface (base), the standalone simulator (sim)
and the client of the signalhound service (remote_sa, used with --real).

There is no vendor driver here on purpose: the Signal Hound SA API allows one
process per analyser, and that process is the signalhound service. So the real
backend touches no USB and claims no hardware lock -- the owner does both."""


def real_backend(cfg):
    """The --real backend -- ONE place, so the service and the GUI's --real can
    never disagree about it. The import stays inside, so the simulator path
    never loads pyzmq's client code for nothing."""
    from .remote_sa import RemoteSa
    return RemoteSa(cfg)
