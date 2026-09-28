"""ppms: a Quantum Design DynaCool (PPMS family) as an AaltoFlow module.

The cryostat's field, temperature and chamber, driven through MultiVu -- the
Python successor of the DynaCool half of the old LabVIEW "QD_VNA_Integration"
program (QDInstrument_ControlField.vi). MultiVu runs the magnet supply and the
temperature controller itself, so this is a set-and-forget module whose one
subtle job is saying honestly when a setpoint has been REACHED:

    config     -- every tunable number as dataclasses, with plain-text save/load.
    backends   -- `base` defines the interface; `sim` is a fake DynaCool so
                  everything runs offline; `multivu` drives the real one through
                  Quantum Design's MultiPyVu (the only file that imports it).
    cryostat   -- the brain: clamps setpoints, pushes them, polls, and decides
                  field_stable / temperature_stable.
    net        -- the ZeroMQ service + a matching client + `describe`.

Units are the suite's (mT, K); MultiVu's oersted never leaves `backends/multivu.py`.
"""

__version__ = "0.1.0"
