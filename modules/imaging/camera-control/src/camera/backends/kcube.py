"""Real Z-focus backend -- Thorlabs KCube piezo via pylablib (Kinesis).

The camera module owns the focus (Z) axis directly (unlike XY, which it drives
through the piezo-control service).  On the LabVIEW rig this is a Thorlabs KCube
piezo controller ("KCubePiezoControl"); pylablib wraps the Kinesis DLLs.

Lazy import inside :meth:`open` (blueprint §3) so the package imports fine with no
Kinesis installed; ``pylablib`` stays COMMENTED OUT in pyproject.toml until the
lab PC.

To finish at the microscope:
  1. Install Thorlabs Kinesis, then `pip install pylablib` (uncomment in pyproject).
  2. Set ``serial`` to your KCube's serial number.
  3. VERIFY the volt<->device-unit mapping: some KPZ101 units command a
     0..max-voltage as a fraction; adjust :meth:`set_z` / :meth:`read_z` to your
     controller's max output (75 V here, matching the LabVIEW panel).

Implements the :class:`camera.backends.base.ZFocus` Protocol.
"""

from __future__ import annotations

from . import claim as hwclaim


class KCubeZFocus:
    def __init__(self, serial: str = "", vmin: float = 0.0, vmax: float = 75.0):
        self.serial = serial
        self._vmin = float(vmin)
        self._vmax = float(vmax)
        self._dev = None
        self._v = 0.0
        self._hwlock = None    # our claim on this KCube's serial (hwlock)

    def open(self) -> None:
        try:
            from pylablib.devices import Thorlabs
        except ImportError as exc:  # pragma: no cover - only on a real PC
            raise RuntimeError(
                "pylablib not installed. `pip install pylablib` and install "
                "Thorlabs Kinesis (see kcube.py header)."
            ) from exc
        # CLAIM the KCube's serial before the first byte goes to it. The same
        # KCube may be configured in zpiezo-control too (the camera's own-KCube
        # path is the FALLBACK for exactly that controller); both claim the bare
        # Kinesis serial, so only one of the two can drive it. An empty serial
        # is refused here: pylablib needs one anyway, and "whichever KCube
        # answers first" is not an address anybody could check against.
        self._hwlock = hwclaim.claim(hwclaim.kinesis_address(self.serial))
        try:
            # KinesisPiezoController wraps KPZ/KCube devices.
            self._dev = Thorlabs.KinesisPiezoController(self.serial)  # VERIFY on the rig
        except BaseException:
            self._release()
            raise

    def close(self) -> None:
        try:
            if self._dev is not None:
                self._dev.close()
        finally:
            self._dev = None
            self._release()

    def _release(self) -> None:
        if self._hwlock is not None:
            self._hwlock.release()
            self._hwlock = None

    def set_z(self, volts: float) -> None:  # pragma: no cover - only on a real PC
        v = max(self._vmin, min(self._vmax, float(volts)))
        self._v = v
        # NOTE: verify units for your controller (V vs fraction of max output).
        self._dev.set_output_voltage(v)

    def read_z(self) -> float:  # pragma: no cover - only on a real PC
        if self._dev is not None:
            try:
                self._v = float(self._dev.get_output_voltage())
            except Exception:
                pass
        return self._v

    def z_range(self) -> tuple:
        return (self._vmin, self._vmax)
