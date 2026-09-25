"""Hardware backends: the interface (base), the simulator (sim) and the real
Keysight PNA-X N5222A over VISA (pna). Only `pna` touches pyvisa, and only
inside `open()`, so the package imports on a PC with no VISA installed."""
