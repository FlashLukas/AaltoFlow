"""A pure-Python simulator of ELL14 rotation mounts on one bus.

It implements every method of :class:`elliptec.backends.base.ElliptecBackend`,
so the whole application -- GUI, service, client, tests -- runs with no
hardware and no pyserial.  This is the DEFAULT backend.

"Just enough physics" to behave like the real mount:
  * each mount turns at a constant angular speed (``sim.max_speed_deg_s``
    times the velocity percentage) -- the resonant piezo motor has no
    noticeable acceleration phase on the time scale of a status frame;
  * the reported angle is QUANTISED to the encoder's pulses (143360 per turn
    on an ELL14), so a readout never shows more precision than the mount has;
  * an absolute move goes the direct way inside the DEVICE frame [0, 360)
    and never crosses the home mark, a relative move just keeps turning --
    which is why the brain sends RELATIVE steps inside an angle window (with
    an offset the window can straddle the home mark);        (VERIFY on the ELL14)
  * homing turns in the chosen direction until the home mark (device 0 deg);
  * a test can inject an Elliptec error code to exercise the error paths.
"""

from __future__ import annotations

import time

from ..config import Config
from .base import AxisReading


class _SimMount:
    def __init__(self, start_deg: float):
        self.pos = float(start_deg)   # continuous angle (not wrapped), degrees
        self.start = self.pos
        self.target = self.pos
        self.t0 = 0.0
        self.moving = False
        self.velocity_pct = 100
        self.error = 0


class SimEllBus:
    """Simulated ELL14K interface board with N mounts."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._mounts: dict[str, _SimMount] = {}
        self._opened = False

    # -- connection -------------------------------------------------------- #
    def open(self, addresses: list) -> None:
        for i, a in enumerate(addresses):
            # Stagger the start angles so two simulated mounts are told apart.
            self._mounts[a] = _SimMount(self.cfg.sim.start_deg + 50.0 * i)
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def idn(self) -> str:
        return f"SIM Elliptec bus ({len(self._mounts)} x ELL14, simulator)"

    def device_info(self, address: str) -> dict:
        self._mount(address)
        return {"address": address, "model": "ELL14 (sim)", "firmware": "sim",
                "travel_deg": 360, "pulses_per_rev": int(self.cfg.sim.pulses_per_rev)}

    # -- internals --------------------------------------------------------- #
    def _mount(self, address: str) -> _SimMount:
        if not self._opened:
            raise RuntimeError("bus is not open")
        m = self._mounts.get(address)
        if m is None:
            # A real bus simply stays silent for an absent address; the
            # driver then times out.  Raising here is the simulator's version.
            raise RuntimeError(f"no mount answers on address {address}")
        return m

    def _speed(self, m: _SimMount) -> float:
        return max(1e-6, self.cfg.sim.max_speed_deg_s * m.velocity_pct / 100.0)

    def _quantise(self, deg: float) -> float:
        ppr = max(1, int(self.cfg.sim.pulses_per_rev))
        return round(deg / 360.0 * ppr) * 360.0 / ppr

    def _advance(self, m: _SimMount) -> None:
        """Integrate the motion up to now (lazily, on every read)."""
        if not m.moving:
            return
        dist = m.target - m.start
        travelled = self._speed(m) * (time.monotonic() - m.t0)
        if travelled >= abs(dist):
            m.pos = m.target
            m.moving = False
        else:
            m.pos = m.start + (travelled if dist >= 0 else -travelled)

    def _go(self, m: _SimMount, target: float) -> None:
        self._advance(m)
        m.start = m.pos
        m.target = float(target)
        m.t0 = time.monotonic()
        m.moving = abs(m.target - m.start) > 1e-9
        m.error = 0

    # -- motion ------------------------------------------------------------ #
    def start_move_abs(self, address: str, device_deg: float) -> None:
        m = self._mount(address)
        self._advance(m)
        # Absolute: the direct way inside one turn.  Re-express the current
        # angle in [0, 360) first, so the continuous counter left behind by
        # relative moves does not make it unwind several turns.
        here = m.pos % 360.0
        m.pos = m.start = here
        self._go(m, float(device_deg) % 360.0)

    def start_move_rel(self, address: str, delta_deg: float) -> None:
        m = self._mount(address)
        self._advance(m)
        self._go(m, m.pos + float(delta_deg))

    def start_home(self, address: str, ccw: bool = False) -> None:
        m = self._mount(address)
        self._advance(m)
        here = m.pos % 360.0
        m.pos = here
        # Turn in the requested direction until the home mark at 0 deg.
        self._go(m, 360.0 if (ccw and here > 0) else 0.0)

    def stop(self, address: str) -> None:
        m = self._mount(address)
        self._advance(m)
        m.moving = False
        m.target = m.pos

    def poll(self, address: str) -> AxisReading:
        m = self._mount(address)
        self._advance(m)
        if not m.moving and m.pos >= 360.0 - 1e-9 and m.pos <= 360.0 + 1e-9:
            m.pos = 0.0          # a ccw home ends exactly on the mark
        return AxisReading(self._quantise(m.pos), m.moving, m.error)

    # -- parameters -------------------------------------------------------- #
    def set_velocity(self, address: str, percent: int) -> None:
        m = self._mount(address)
        self._advance(m)
        # Re-anchor a move in flight at the current angle and time, so the new
        # speed applies from NOW and the angle does not jump (kim's gotcha #10).
        m.start = m.pos
        m.t0 = time.monotonic()
        m.velocity_pct = int(percent)

    def read_velocity(self, address: str) -> int:
        return self._mount(address).velocity_pct

    # -- test hooks -------------------------------------------------------- #
    def inject_error(self, address: str, code: int) -> None:
        """Make a mount report an Elliptec error code and stop (tests)."""
        m = self._mount(address)
        self._advance(m)
        m.moving = False
        m.error = int(code)
