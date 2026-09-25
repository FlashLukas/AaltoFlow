"""Real Z backend -- Thorlabs KCube piezo via pylablib (Kinesis).

The ONLY file that imports the hardware library, and it imports LAZILY inside
:meth:`open` so the package still imports and the simulator still runs on a PC
with no Kinesis.  ``pylablib`` stays COMMENTED OUT in pyproject.toml until the lab
PC.

To finish at the microscope:
  1. Install Thorlabs Kinesis, then `pip install pylablib` (uncomment in pyproject).
  2. Set ``serial`` to your KCube's serial number.
  3. VERIFY the volt<->device-unit mapping for your controller (some KPZ101 units
     command a 0..max-voltage as a fraction); adjust set/read accordingly.
"""

from __future__ import annotations


class KCubeZ:
    def __init__(self, serial: str = "", v_min: float = 0.0, v_max: float = 75.0):
        self.serial = serial
        self._vmin = float(v_min)
        self._vmax = float(v_max)
        self._dev = None
        self._v = 0.0

    def open(self) -> None:
        try:
            from pylablib.devices import Thorlabs
        except ImportError as exc:  # pragma: no cover - only on a real PC
            raise RuntimeError(
                "pylablib not installed. `pip install pylablib` and install "
                "Thorlabs Kinesis (see kcube.py header)."
            ) from exc
        self._dev = Thorlabs.KinesisPiezoController(self.serial)  # pragma: no cover

    def close(self) -> None:  # pragma: no cover - only on a real PC
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    def idn(self) -> str:
        return f"Thorlabs KCube piezo {self.serial!r}"

    def set_voltage(self, volts: float) -> None:  # pragma: no cover - real PC
        self._v = float(volts)
        self._dev.set_output_voltage(self._v)

    def read_voltage(self) -> float:  # pragma: no cover - real PC
        if self._dev is not None:
            try:
                self._v = float(self._dev.get_output_voltage())
            except Exception:
                pass
        return self._v

    def range(self) -> tuple:
        return (self._vmin, self._vmax)
