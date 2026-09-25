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


def build_sim_system(cfg: Config):
    """Create a Controller on simulated hardware, with a calibration ready.

    Returns (controller, kepco, probe, acq, calibration).
    """
    cfg.pid.Kc_A_per_mT = 0.01     # sim-tuned gains (see README)
    cfg.pid.Ti_s = 0.15
    kepco = SimulatedKepco(current_max_A=cfg.limits.current_max_A)
    probe = SimulatedHallProbe(kepco, hall=cfg.hall)
    kepco.open()

    # quick synchronous calibration so the field controls work immediately
    probe.emulate_timing = False
    I_max = cfg.limits.current_max_A
    up = [(-I_max) + 2 * I_max * i / 49 for i in range(50)]
    raw = []
    for I in up + list(reversed(up)):
        kepco.set_current(I)
        v = probe.read_voltage(cfg.acquisition.precise_samples, cfg.acquisition.precise_rate_Hz)
        raw.append((I, cfg.hall.volts_to_field(v)))
    probe.emulate_timing = True
    cal = FieldCalibration.from_sweep(raw, hall=cfg.hall)

    aux = SimulatedAux(v_min=cfg.aux.v_min, v_max=cfg.aux.v_max)
    acq = AcquisitionThread(probe, cfg.hall, cfg.acquisition)
    ctrl = Controller(cfg, kepco, acq, calibration=cal, aux=aux)
    return ctrl, kepco, probe, acq, cal
