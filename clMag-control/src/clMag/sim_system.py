"""Build a ready-to-run controller wired to the simulator.

Kept free of any GUI/Qt import so the headless service (and tests) can use it
without pulling in PySide6.
"""

from __future__ import annotations

from .config import Config
from .calibration import FieldCalibration
from .backends.sim import SimulatedKepco, SimulatedHallProbe, SimulatedAux
from .acquisition import AcquisitionThread
from .controller import Controller


def sim_calibration(cfg: Config) -> FieldCalibration:
    """A B(I) calibration measured on a SCRATCH copy of the simulated magnet.

    It used to be swept on the very supply the controller then drove, which
    left that "instrument" at -I_max before the service had even started. A
    real service would load a saved calibration file instead of sweeping the
    magnet at start, so the sim sweeps a private twin with the same physics
    and leaves the supply the controller adopts untouched.
    """
    kepco = SimulatedKepco(current_max_A=cfg.limits.current_max_A, output_on=True)
    probe = SimulatedHallProbe(kepco, hall=cfg.hall, emulate_timing=False)
    I_max = cfg.limits.current_max_A
    up = [(-I_max) + 2 * I_max * i / 49 for i in range(50)]
    raw = []
    for I in up + list(reversed(up)):
        kepco.set_current(I)
        v = probe.read_voltage(cfg.acquisition.precise_samples, cfg.acquisition.precise_rate_Hz)
        raw.append((I, cfg.hall.volts_to_field(v)))
    return FieldCalibration.from_sweep(raw, hall=cfg.hall)


def build_sim_system(cfg: Config, *, initial_current_A: float = 0.0,
                     output_on: bool = False, initial_do: dict | None = None):
    """Create a Controller on simulated hardware, with a calibration ready.

    `initial_current_A` / `output_on` / `initial_do` are the state the
    simulated instruments are in BEFORE the service connects -- the tests use
    them to check that start-up adopts that state instead of resetting it.
    The default is a supply at rest (output off, 0 A).

    Returns (controller, kepco, probe, acq, calibration).
    """
    cfg.pid.Kc_A_per_mT = 0.01     # sim-tuned gains (see README)
    cfg.pid.Ti_s = 0.15
    cal = sim_calibration(cfg)
    kepco = SimulatedKepco(current_max_A=cfg.limits.current_max_A,
                           initial_current_A=initial_current_A, output_on=output_on)
    probe = SimulatedHallProbe(kepco, hall=cfg.hall)

    aux = SimulatedAux(v_min=cfg.aux.v_min, v_max=cfg.aux.v_max, initial_do=initial_do)
    acq = AcquisitionThread(probe, cfg.hall, cfg.acquisition)
    ctrl = Controller(cfg, kepco, acq, calibration=cal, aux=aux)
    return ctrl, kepco, probe, acq, cal
