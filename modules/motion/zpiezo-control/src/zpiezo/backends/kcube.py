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

from .. import hwlock

# KPZ101 (KCube piezo) serial numbers start with "29" -- used only when no
# serial is configured, to pick the controller out of every Kinesis device on
# the USB bus.  VERIFY: the prefix on the lab's unit (Kinesis shows it).
_KPZ_PREFIX = "29"


class KCubeZ:
    def __init__(self, serial: str = "", v_min: float = 0.0, v_max: float = 75.0):
        self.serial = serial
        self._vmin = float(v_min)
        self._vmax = float(v_max)
        self._dev = None
        self._v = 0.0
        # The claim on this KCube's serial number (hwlock).  Lukas's rule: the
        # same instrument is defined by the same physical address, and only ONE
        # service may drive it -- two programs setting one focus voltage would
        # each think they know where the focus is.  Held from open() to close().
        self._hw_lock: hwlock.HardwareLock | None = None

    def open(self) -> None:
        try:
            from pylablib.devices import Thorlabs
        except ImportError as exc:  # pragma: no cover - only on a real PC
            raise RuntimeError(
                "pylablib not installed. `pip install pylablib` and install "
                "Thorlabs Kinesis (see kcube.py header)."
            ) from exc
        # Which physical box?  The configured serial, else the first KPZ101 on
        # the bus.  Listing devices opens none of them, so claiming right after
        # it still comes before the first byte goes to the controller.
        serial = (self.serial or "").strip() or self._find_kcube(Thorlabs)
        self.serial = serial
        # Claim BEFORE opening.  Raises hwlock.HardwareBusy (naming the holder)
        # when any other service already drives this KCube; we then never touch it.
        self._hw_lock = hwlock.claim(serial, "zpiezo")
        try:
            # Opening must NOT change the output (adopt-on-start rule): only the
            # constructor here, no set_*/zero/enable call.  The brain then READS
            # the voltage the KCube is already holding and adopts it as the focus.
            # VERIFY: that pylablib's KinesisPiezoController() constructor sends no
            # state-changing message (it should only open the USB handle).
            self._dev = Thorlabs.KinesisPiezoController(serial)
        except BaseException:
            # A failed open must not leave the serial claimed -- the next start
            # would otherwise report the KCube "busy", held by ourselves.
            self.close()
            raise

    @staticmethod
    def _find_kcube(thorlabs) -> str:
        # VERIFY: list_kinesis_devices() returns (serial, description) pairs.
        found = [str(conn) for conn, _desc in thorlabs.list_kinesis_devices()
                 if str(conn).startswith(_KPZ_PREFIX)]
        if not found:
            raise RuntimeError(
                f"no KCube piezo found (serials starting with {_KPZ_PREFIX}). "
                "Is it plugged in and closed in the Kinesis app? Or set "
                "hardware.serial in the config.")
        return found[0]

    def close(self) -> None:
        try:
            if self._dev is not None:
                dev, self._dev = self._dev, None
                dev.close()
        finally:
            # Release the claim LAST, after the handle is closed, so no other
            # service can open the KCube while we still hold the USB link.
            if self._hw_lock is not None:
                self._hw_lock.release()
                self._hw_lock = None

    def idn(self) -> str:
        return f"Thorlabs KCube piezo {self.serial!r}"

    def set_voltage(self, volts: float) -> None:  # pragma: no cover - real PC
        self._v = float(volts)
        self._dev.set_output_voltage(self._v)

    def read_voltage(self) -> float:  # pragma: no cover - real PC
        # A failed read RAISES (it used to return the last cached value, 0 V
        # before the first move).  Returning 0 silently would make the brain
        # adopt a focus of 0 V that the KCube is not holding; the brain's
        # status() already catches the exception and falls back to the target.
        # VERIFY: get_output_voltage() returns volts (not a fraction of max).
        if self._dev is None:
            raise RuntimeError("KCube not open")
        self._v = float(self._dev.get_output_voltage())
        return self._v

    def range(self) -> tuple:
        return (self._vmin, self._vmax)
