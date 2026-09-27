"""The REAL backend: a SmarAct SCU controller through its C library (ctypes).

This is the ONLY file in the package that touches the vendor library, and it
loads it lazily inside :meth:`ScuStage.open`, so the package imports (and the
simulator runs) on a PC without the SmarAct software.

No pip dependency: the SCU software ships a DLL (``SCU3DControl.dll`` for the
SCU family) that is called directly with :mod:`ctypes`.

Sources used for the function names and signatures (NOT the vendor manual,
which was not available when this was written -- hence every call is marked
# VERIFY):

* pylablib ``devices/SmarAct/SCU3DControl_lib.py`` / ``SCU3DControl_defs.py`` /
  ``scu3d.py`` (Alexey Shkarin): the complete list of exported functions with
  their C argument types, the status codes (SA_STOPPED_STATUS = 0 ...
  SA_MOVING_TO_REFERENCE_STATUS = 6), the error codes, and the open sequence
  ``SA_InitDevices(SA_SYNCHRONOUS_COMMUNICATION = 0)`` / ``SA_ReleaseDevices``.
* EPICS motorSmarAct ``docs/README.SmarActSCU`` + ``smarActSCUMotorDriver.cpp``
  (ASCII protocol of the same controller): closed-loop moves carry a HOLD time,
  ``MTR`` = move to reference with hold time and an auto-zero flag, the
  "physical position known" bit survives a reconnect as long as the SCU stays
  powered, and the sensor resolves 0.1 um.

What is NOT confirmed and must be checked on the instrument (grep "# VERIFY"):

1. The DLL name for a single-channel SCU (SCU3DControl.dll assumed).
2. The unit of the integer position of SA_GetPosition_S / SA_MovePosition*_S
   (hardware.nm_per_count, 100 nm assumed from the 0.1 um sensor resolution).
3. Whether a move command returns before the status turns to "targeting"
   (the brain covers the gap with a short grace window either way).
4. The hold-time semantics (0 = no hold assumed; what value means "forever").
5. The allowed closed-loop max frequency range.
6. The sensor type stored in the SCU (hardware.sensor_type, 0 = do not check;
   non-zero = CHECK it at start, never set it).
7. The direction MoveToReference searches in, and whether autoZero=0 keeps the
   absolute scale of the distance-coded marks (assumed yes).
"""

from __future__ import annotations

import ctypes
import threading

from ..config import Config
from .base import CHANNEL_STATES

SA_OK = 0
SA_SYNCHRONOUS_COMMUNICATION = 0

#: Error codes worth a readable name (from SCU3DControl_defs, see docstring).
_ERRORS = {
    1: "INITIALIZATION_ERROR", 2: "NOT_INITIALIZED_ERROR",
    3: "NO_DEVICES_FOUND_ERROR", 5: "INVALID_DEVICE_INDEX_ERROR",
    6: "INVALID_CHANNEL_INDEX_ERROR", 7: "TRANSMIT_ERROR", 9: "INVALID_PARAMETER_ERROR",
    10: "READ_ERROR", 13: "WRONG_MODE_ERROR", 15: "TIMEOUT_ERROR",
    128: "INVALID_COMMAND_ERROR", 129: "COMMAND_NOT_SUPPORTED_ERROR",
    130: "NO_SENSOR_PRESENT_ERROR", 131: "WRONG_SENSOR_TYPE_ERROR",
    132: "END_STOP_REACHED_ERROR", 133: "COMMAND_OVERRIDDEN_ERROR",
}


class ScuError(RuntimeError):
    pass


class ScuStage:
    """One channel of a SmarAct SCU driving a linear positioner with a sensor."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lib = None
        self._dev = int(cfg.hardware.device_index)
        self._ch = int(cfg.hardware.channel)
        # The DLL is not documented as thread-safe; the brain already calls us
        # from one lock, this one is a second belt.
        self._lock = threading.RLock()
        self._freq = 0

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _call(self, name: str, *args) -> None:
        if self._lib is None:
            raise ScuError("SCU library not open")
        with self._lock:
            rc = getattr(self._lib, name)(*args)
        if rc != SA_OK:
            raise ScuError(f"{name} failed: {rc} ({_ERRORS.get(rc, 'UNKNOWN')})")

    def _sign(self) -> int:
        return -1 if self.cfg.hardware.invert else 1

    def _mm_to_counts(self, mm: float) -> int:
        return int(round(self._sign() * mm * 1e6 / self.cfg.hardware.nm_per_count))

    def _counts_to_mm(self, counts: int) -> float:
        return self._sign() * counts * self.cfg.hardware.nm_per_count * 1e-6

    # ------------------------------------------------------------------ #
    # connection
    # ------------------------------------------------------------------ #
    def open(self) -> None:
        path = self.cfg.hardware.dll_path
        try:
            lib = ctypes.CDLL(path)  # cdecl, as pylablib loads it  # VERIFY
        except OSError as exc:
            raise ScuError(
                f"cannot load the SmarAct SCU library {path!r} ({exc}). Install "
                "the SCU software from the controller's CD, or set "
                "hardware.dll_path to the DLL.") from exc
        u, pu, pi = ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_int)
        sigs = {
            "SA_InitDevices": [u],
            "SA_ReleaseDevices": [],
            "SA_GetNumberOfDevices": [pu],
            "SA_GetDeviceID": [u, pu],
            "SA_GetDeviceFirmwareVersion": [u, pu],
            "SA_GetSensorPresent_S": [u, u, pu],
            "SA_GetSensorType_S": [u, u, pu],
            "SA_MovePositionAbsolute_S": [u, u, ctypes.c_int, u],
            "SA_MovePositionRelative_S": [u, u, ctypes.c_int, u],
            "SA_MoveToReference_S": [u, u, u, u],
            "SA_Stop_S": [u, u],
            "SA_GetStatus_S": [u, u, pu],
            "SA_GetPosition_S": [u, u, pi],
            "SA_GetPhysicalPositionKnown_S": [u, u, pu],
            "SA_SetClosedLoopMaxFrequency_S": [u, u, u],
            "SA_GetClosedLoopMaxFrequency_S": [u, u, pu],
        }
        for name, argtypes in sigs.items():
            fn = getattr(lib, name)
            fn.argtypes = argtypes
            fn.restype = ctypes.c_uint
        self._lib = lib

        self._call("SA_InitDevices", SA_SYNCHRONOUS_COMMUNICATION)  # VERIFY
        n = ctypes.c_uint(0)
        self._call("SA_GetNumberOfDevices", ctypes.byref(n))  # VERIFY
        if self._dev >= n.value:
            self.close()
            raise ScuError(f"SCU device index {self._dev} not found ({n.value} connected)")
        # Startup READS and never writes (Lukas's rule, 2026-09-27): the
        # sensor type is a setting stored in the SCU, so it is only CHECKED
        # here. A wrong type means every position reading is wrong, hence the
        # refusal instead of a warning; set it once with SmarAct's own tool,
        # or leave hardware.sensor_type = 0 to skip the check.
        want = int(self.cfg.hardware.sensor_type)
        if want:
            have = ctypes.c_uint(0)
            self._call("SA_GetSensorType_S", self._dev, self._ch,
                       ctypes.byref(have))  # VERIFY (newly used query)
            if have.value != want:
                self.close()
                raise ScuError(
                    f"the SCU channel is configured for sensor type {have.value}, "
                    f"hardware.sensor_type asks for {want}. The service does not "
                    "change it at start; set it with the SmarAct software, or set "
                    "hardware.sensor_type = 0 to accept the controller's setting.")
        if not self.sensor_present():
            self.close()
            raise ScuError("the SCU reports NO position sensor on this channel; "
                           "closed-loop control is impossible")
        # Only QUERIES from here on: the brain adopts the frequency, position,
        # state and "position known" the controller already has.
        self._freq = self.get_max_frequency()

    def close(self) -> None:
        if self._lib is None:
            return
        try:
            with self._lock:
                self._lib.SA_ReleaseDevices()  # VERIFY
        finally:
            self._lib = None

    def idn(self) -> str:
        if self._lib is None:
            return "SmarAct SCU (not open)"
        dev_id, fw = ctypes.c_uint(0), ctypes.c_uint(0)
        try:
            self._call("SA_GetDeviceID", self._dev, ctypes.byref(dev_id))  # VERIFY
            self._call("SA_GetDeviceFirmwareVersion", self._dev, ctypes.byref(fw))  # VERIFY
        except ScuError:
            return "SmarAct SCU"
        v = fw.value
        fw_txt = ".".join(str((v >> s) & 0xFF) for s in (24, 16, 8, 0))
        return f"SmarAct SCU id {dev_id.value}, firmware {fw_txt}, channel {self._ch}"

    def sensor_present(self) -> bool:
        p = ctypes.c_uint(0)
        self._call("SA_GetSensorPresent_S", self._dev, self._ch, ctypes.byref(p))  # VERIFY
        return bool(p.value)

    # ------------------------------------------------------------------ #
    # motion
    # ------------------------------------------------------------------ #
    def move_absolute(self, position_mm: float, hold_ms: int) -> None:
        self._call("SA_MovePositionAbsolute_S", self._dev, self._ch,
                   self._mm_to_counts(position_mm), int(hold_ms))  # VERIFY units

    def move_relative(self, delta_mm: float, hold_ms: int) -> None:
        self._call("SA_MovePositionRelative_S", self._dev, self._ch,
                   self._mm_to_counts(delta_mm), int(hold_ms))  # VERIFY units

    def find_reference(self, hold_ms: int) -> None:
        # autoZero = 0: keep the absolute scale the distance-coded marks give.
        self._call("SA_MoveToReference_S", self._dev, self._ch, int(hold_ms), 0)  # VERIFY

    def stop(self) -> None:
        self._call("SA_Stop_S", self._dev, self._ch)  # VERIFY

    # ------------------------------------------------------------------ #
    # read-back
    # ------------------------------------------------------------------ #
    def read_position_mm(self) -> float:
        p = ctypes.c_int(0)
        self._call("SA_GetPosition_S", self._dev, self._ch, ctypes.byref(p))  # VERIFY
        return self._counts_to_mm(p.value)

    def channel_state(self) -> str:
        s = ctypes.c_uint(0)
        self._call("SA_GetStatus_S", self._dev, self._ch, ctypes.byref(s))  # VERIFY
        return CHANNEL_STATES[s.value] if s.value < len(CHANNEL_STATES) else f"code_{s.value}"

    def physical_position_known(self) -> bool:
        k = ctypes.c_uint(0)
        self._call("SA_GetPhysicalPositionKnown_S", self._dev, self._ch, ctypes.byref(k))  # VERIFY
        return bool(k.value)

    # ------------------------------------------------------------------ #
    # speed
    # ------------------------------------------------------------------ #
    def set_max_frequency(self, hz: int) -> None:
        self._call("SA_SetClosedLoopMaxFrequency_S", self._dev, self._ch, int(hz))  # VERIFY range
        self._freq = int(hz)

    def get_max_frequency(self) -> int:
        f = ctypes.c_uint(0)
        self._call("SA_GetClosedLoopMaxFrequency_S", self._dev, self._ch, ctypes.byref(f))  # VERIFY
        return int(f.value)
