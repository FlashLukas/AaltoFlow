"""vna: a vector network analyser -- a Keysight PNA-X N5222A or a Copper
Mountain C1209, or a simulator of one measuring S-parameters of a YIG film.

For VNA-FMR: magnet field against a hardware-swept frequency trace, complex
data, relative to a reference trace (u = (S - S_ref)/S_ref).

    config    -- every tunable number as dataclasses, with .ini save/load.
    model     -- the simulator's physics: Kittel (with uniaxial anisotropy),
                 susceptibility, the line, noise, S11/S12/S21/S22.
    field     -- where the field is READ from: a magnet's status stream (mag2d,
                 mag2dcal, clMag, the DynaCool's ppms), or manual.
    backends  -- `base` (the interface), `sim` (the simulator), `pna` (the real
                 PNA-X) and `cmt` (the real C1209), both over pyvisa and
                 imported only when opened; `real_backend(cfg)` picks one.
    analyzer  -- the brain: clamps settings, owns the sweep thread, the field
                 subscription, the scan-safe `acquire` and the reference.
    net       -- the ZeroMQ service, client and `describe` manifest.
"""

__version__ = "0.1.0"
