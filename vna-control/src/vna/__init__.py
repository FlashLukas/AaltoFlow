"""vna: a vector network analyser -- the Keysight PNA-X N5222A, or a simulator
of one measuring S-parameters of a YIG film.

For VNA-FMR: magnet field against a hardware-swept frequency trace, complex
data, relative to a reference trace (u = (S - S_ref)/S_ref).

    config    -- every tunable number as dataclasses, with .ini save/load.
    model     -- the simulator's physics: Kittel (with uniaxial anisotropy),
                 susceptibility, the line, noise, S11/S12/S21/S22.
    field     -- where the field is READ from: mag2d's or clMag's status stream, or manual.
    backends  -- `base` (the interface), `sim` (the simulator), `pna` (the real
                 PNA-X over pyvisa, imported only when opened).
    analyzer  -- the brain: clamps settings, owns the sweep thread, the field
                 subscription, the scan-safe `acquire` and the reference.
    net       -- the ZeroMQ service, client and `describe` manifest.
"""

__version__ = "0.1.0"
