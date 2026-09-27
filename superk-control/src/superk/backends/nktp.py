"""The REAL SuperK EXTREME + SELECT RF driver, through NKT's NKTPDLL.dll.

THIS IS THE ONLY FILE THAT TOUCHES THE VENDOR LIBRARY. The DLL is loaded with
ctypes inside `open()`, so the package imports (and the simulator runs) on a PC
without the NKT SDK.

How the SuperK talks
--------------------
NKT systems use "Interbus": one serial port (the EXTREME's USB virtual COM
port), several MODULES on it, each with an address, each exposing numbered
REGISTERS of a fixed type (U8/U16/S16/U32). Everything is "read register /
write register" -- there are no text commands.

    openPorts(ports, autoMode, liveMode)                    open the COM port
    registerReadU8/U16/S16/U32(port, devId, regId, &value, index)
    registerWriteU8/U16/U32(port, devId, regId, value, index)
    deviceGetAllTypes(port, types, &maxTypes)               module type per address
    closePorts(ports)

Every function returns a result code; 0 means success.

Sources used (none of this has been run against the lab's laser yet):
  * NKT Photonics "SDK Instruction manual" (the PDF shipped with the SDK),
    section 6.8 "SuperK EXTREME System (S4x2) and SuperK Fianium" (main
    module, type 60h, standard address 15), section 6.9 "RF Driver (A901) for
    SuperK SELECT" (type 66h; internal driver at address 6, an external one at
    16 + its rotary switch) and section 6.10 "SuperK SELECT (A203)" (type 67h,
    16 + rotary switch), plus the SDK register files 60.txt / 66.txt / 67.txt.
    Checked there:
      EXTREME 11h inlet temp I16 0.1 C; 30h emission U8 0=off 3=on; 32h
      interlock U16 (write >0 = reset; read LSB 0 open / 1 waiting for reset /
      2 OK, MSB = where it is open); 36h watchdog U8 s (0 = off); 37h power
      level U16 0.1 % (constant-power mode; 38h is the current level); 66h
      status bits (bit 0 emission on, bit 1 interlock relays off).
      RF driver 30h RF power U8 0/1; 34h/35h min/max wavelength of the
      CONNECTED crystal U32 pm (read-only); 38h crystal temp I16 0.1 C;
      75h "connected crystal" U8, READ-ONLY (1, 2 = the crystals of the SELECT
      with the lowest bus address, 3, 4 = the next SELECT, 0 = none);
      90h-97h wavelength U32 pm (a 4-element FSK array; writing only
      element 0 is allowed); B0h-B7h amplitude U16 0.1 %.
      SELECT housing 34h "RF switch" U8: 0 = normal, 1 = swap the two RF
      inputs; RF power must be OFF before it is changed.
    CONSEQUENCE: a crystal is NOT chosen by writing the RF driver (75h is only
    a readback). It is chosen with the RF switch of the SELECT housing that
    holds it -- and a crystal in ANOTHER housing is reachable only by moving
    the RF cable by hand. select_crystal() below does what the hardware allows
    and then checks 75h; if the driver still reports another crystal it
    raises, telling the operator to move the cable.
  * NKT's ctypes wrapper NKTP_DLL.py (<sdk>/NKTPDLL/x64/NKTPDLL.dll, found via
    the NKTP_SDK_PATH environment variable): every function returns an
    UNSIGNED CHAR result code (0 = success); the index argument is a c_short,
    -1 = the whole register.
  * Cross-checked against pylablib's NKT docs (emission 0x30 = 3, power 0x37 in
    0.1 %, inlet temperature 0x11 in 0.1 C) and the open-source nkt_tools
    package (Extreme / Select / RF driver classes, same registers).

Every call not confirmed on OUR hardware is marked # VERIFY.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from ..config import N_LINES

# ---- module type codes (register 0x61 of every module) ---------------------
TYPE_EXTREME = 0x60           # SuperK EXTREME main module          # VERIFY
TYPE_SELECT_RF = 0x66         # RF driver A901 (drives the SELECTs)  # VERIFY
TYPE_SELECT = 0x67            # SuperK SELECT housing A203 (RF switch) # VERIFY

# ---- EXTREME registers -----------------------------------------------------
REG_INLET_TEMP = 0x11         # S16, 0.1 C                            # VERIFY
REG_EMISSION = 0x30           # U8: 0 = off, 3 = on                   # VERIFY
REG_INTERLOCK = 0x32          # U16: write 1 = reset; read LSB = state # VERIFY
REG_WATCHDOG = 0x36           # U8, seconds, 0 = disabled             # VERIFY
REG_POWER = 0x37              # U16, 0.1 % (constant-power mode)      # VERIFY
REG_STATUS = 0x66             # U16 status bits                       # VERIFY

# ---- SELECT RF driver registers ---------------------------------------------
REG_RF_POWER = 0x30           # U8: 0 = RF off, 1 = RF on              # VERIFY
REG_MIN_WL = 0x34             # U32, pm, range of the active crystal  # VERIFY
REG_MAX_WL = 0x35             # U32, pm                               # VERIFY
REG_XTAL_TEMP = 0x38          # S16, 0.1 C                            # VERIFY
REG_CONNECTED_XTAL = 0x75     # U8, READ-ONLY: which crystal the RF reaches  # VERIFY

# ---- SELECT housing registers (type 0x67) ----------------------------------
REG_RF_SWITCH = 0x34          # U8: 0 = normal, 1 = swap the two RF inputs  # VERIFY

# ---- SELECT RF driver line registers (addressed at rf_addr, like above) ----
REG_WL0 = 0x90                # U32, pm, channel 1 .. 8 = 0x90 .. 0x97  # VERIFY
REG_AMP0 = 0xB0               # U16, 0.1 %, channel 1 .. 8 = 0xB0 .. 0xB7  # VERIFY

EMISSION_ON = 3               # "3" is what NKT's examples write     # VERIFY


class NKTError(RuntimeError):
    pass


class NktpSuperK:
    """SuperK EXTREME + SELECT RF driver over NKTPDLL (ctypes)."""

    def __init__(self, port: str, extreme_addr: int = 15, rf_addr: int = 16,
                 autodetect: bool = True, dll_path: str = ""):
        self.port = port
        self.extreme_addr = int(extreme_addr)
        self.rf_addr = int(rf_addr)
        self.autodetect = bool(autodetect)
        self.dll_path = dll_path
        self._dll = None
        self._port_b = port.encode("ascii")
        self._types = {}
        # bus addresses of the SELECT housings, lowest first: NKT numbers the
        # crystals 1, 2 in the first and 3, 4 in the second (register 75h).
        self.select_addrs: list[int] = []

    # ---- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        self._dll = self._load_dll()
        r = self._dll.openPorts(self._port_b, 1, 0)       # autoMode 1, liveMode 0  # VERIFY
        if r != 0:
            raise NKTError(f"openPorts({self.port}) failed, result {r}")
        if self.autodetect:
            self._find_modules()
        # Deliberately NO emission / RF writes here: open() only connects.

    def close(self) -> None:
        if self._dll is None:
            return
        # emission OFF and RF OFF first; each guarded so one failure cannot
        # stop the other or the port from closing.
        for fn in (lambda: self.set_emission(False), lambda: self.set_rf(False)):
            try:
                fn()
            except Exception:
                pass
        try:
            self._dll.closePorts(self._port_b)
        finally:
            self._dll = None

    def identify(self) -> str:
        if self._dll is None:
            return ""
        sel = ",".join(str(a) for a in self.select_addrs) or "?"
        return (f"SuperK EXTREME @ {self.port}:{self.extreme_addr}, "
                f"RF driver @ {self.rf_addr}, SELECT @ {sel}")

    # ---- EXTREME -----------------------------------------------------------

    def set_emission(self, on: bool) -> None:
        self._w8(self.extreme_addr, REG_EMISSION, EMISSION_ON if on else 0)   # VERIFY

    def read_emission(self) -> bool:
        return bool(self.read_status_bits() & 0x0001)                        # VERIFY bit 0

    def read_interlock(self) -> int:
        return self._r16(self.extreme_addr, REG_INTERLOCK) & 0xFF            # VERIFY LSB = state

    def reset_interlock(self) -> None:
        self._w16(self.extreme_addr, REG_INTERLOCK, 1)                       # VERIFY

    def read_status_bits(self) -> int:
        return self._r16(self.extreme_addr, REG_STATUS)                      # VERIFY

    def set_power(self, pct: float) -> None:
        self._w16(self.extreme_addr, REG_POWER, int(round(pct * 10)))        # VERIFY 0.1 %

    def read_power(self) -> float:
        return self._r16(self.extreme_addr, REG_POWER) / 10.0                # VERIFY

    def read_inlet_temp(self) -> float:
        return self._rs16(self.extreme_addr, REG_INLET_TEMP) / 10.0          # VERIFY

    def set_watchdog(self, seconds: int) -> None:
        self._w8(self.extreme_addr, REG_WATCHDOG, max(0, min(255, int(seconds))))  # VERIFY

    def read_watchdog(self) -> int:
        return self._r8(self.extreme_addr, REG_WATCHDOG)                     # VERIFY readable

    # ---- RF driver ---------------------------------------------------------

    def set_rf(self, on: bool) -> None:
        self._w8(self.rf_addr, REG_RF_POWER, 1 if on else 0)                 # VERIFY

    def read_rf(self) -> bool:
        return bool(self._r8(self.rf_addr, REG_RF_POWER))                    # VERIFY

    def select_crystal(self, crystal: int) -> None:
        """Make the RF driver reach crystal `crystal` (NKT's numbering, see the
        module docstring). The brain switches RF off before calling this, as
        the manual requires; we check once more, because switching under RF
        power is exactly what the manual forbids."""
        crystal = int(crystal)
        if self.read_crystal() == crystal:
            return                                   # already there: touch nothing
        if self.read_rf():
            raise NKTError("RF power must be off before the crystal is changed")
        house, slot = divmod(crystal - 1, 2)         # which SELECT, which crystal in it
        if 0 <= house < len(self.select_addrs):
            addr = self.select_addrs[house]
            # Which switch position reaches `slot` depends on which of the two
            # RF inputs the cable is plugged into, so try both and let the
            # driver's own readback (75h) decide.
            for pos in (slot, 1 - slot):
                self._w8(addr, REG_RF_SWITCH, pos)                            # VERIFY
                if self.read_crystal() == crystal:                            # VERIFY settle time
                    return
        got = self.read_crystal()
        raise NKTError(f"crystal {crystal} is not reachable (the RF driver reports "
                       f"crystal {got}): move the RF cable to SuperK SELECT "
                       f"#{house + 1}")

    def read_crystal(self) -> int:
        return self._r8(self.rf_addr, REG_CONNECTED_XTAL)                    # VERIFY

    def read_crystal_range(self):
        lo = self._r32(self.rf_addr, REG_MIN_WL) / 1000.0                    # VERIFY pm
        hi = self._r32(self.rf_addr, REG_MAX_WL) / 1000.0                    # VERIFY pm
        if hi <= lo or hi <= 0:
            return None
        return lo, hi

    def read_crystal_temp(self) -> float:
        return self._rs16(self.rf_addr, REG_XTAL_TEMP) / 10.0                # VERIFY

    def set_wavelength(self, ch: int, nm: float) -> None:
        self._check_ch(ch)
        self._w32(self.rf_addr, REG_WL0 + ch, int(round(nm * 1000)))         # VERIFY pm

    def read_wavelength(self, ch: int) -> float:
        self._check_ch(ch)
        return self._r32(self.rf_addr, REG_WL0 + ch) / 1000.0                # VERIFY

    def set_amplitude(self, ch: int, pct: float) -> None:
        self._check_ch(ch)
        self._w16(self.rf_addr, REG_AMP0 + ch, int(round(pct * 10)))         # VERIFY 0.1 %

    def read_amplitude(self, ch: int) -> float:
        self._check_ch(ch)
        return self._r16(self.rf_addr, REG_AMP0 + ch) / 10.0                 # VERIFY

    # ---- internals ---------------------------------------------------------

    def _load_dll(self):
        """Find and load NKTPDLL.dll. Lazy, so a PC without the SDK still runs sim."""
        candidates = []
        if self.dll_path:
            candidates.append(Path(self.dll_path))
        sdk = os.environ.get("NKTP_SDK_PATH")
        if sdk:
            candidates.append(Path(sdk) / "NKTPDLL" / "x64" / "NKTPDLL.dll")  # VERIFY layout
        candidates.append(Path("NKTPDLL.dll"))
        last = None
        for c in candidates:
            try:
                dll = ctypes.CDLL(str(c))
                break
            except OSError as exc:
                last = exc
        else:
            raise NKTError("NKTPDLL.dll not found: install the NKT SDK (sets "
                           "NKTP_SDK_PATH) or set hardware.dll_path") from last
        # Signatures after NKTP_DLL.py. # VERIFY against the installed SDK.
        c_char_p, c_ubyte, c_short = ctypes.c_char_p, ctypes.c_ubyte, ctypes.c_short
        # Every NKTPDLL function returns an UNSIGNED CHAR result code. ctypes
        # assumes int unless told otherwise, and then the upper three bytes of
        # the return register are undefined -- "success" could read as an error.
        dll.openPorts.argtypes = [c_char_p, c_ubyte, c_ubyte]
        dll.openPorts.restype = c_ubyte
        dll.closePorts.argtypes = [c_char_p]
        dll.closePorts.restype = c_ubyte
        for name, ctype in (("U8", ctypes.c_ubyte), ("U16", ctypes.c_ushort),
                            ("S16", ctypes.c_short), ("U32", ctypes.c_ulong)):
            rd = getattr(dll, "registerRead" + name)
            rd.argtypes = [c_char_p, c_ubyte, c_ubyte, ctypes.POINTER(ctype), c_short]
            rd.restype = c_ubyte
            if name != "S16":
                wr = getattr(dll, "registerWrite" + name)
                wr.argtypes = [c_char_p, c_ubyte, c_ubyte, ctype, c_short]
                wr.restype = c_ubyte
        dll.deviceGetAllTypes.argtypes = [c_char_p, ctypes.POINTER(ctypes.c_char),
                                          ctypes.POINTER(c_ubyte)]
        dll.deviceGetAllTypes.restype = c_ubyte
        return dll

    def _find_modules(self) -> None:
        """Look up the module addresses by type code, so a system whose modules
        sit at other addresses than NKT's usual ones still works."""
        buf = ctypes.create_string_buffer(256)
        n = ctypes.c_ubyte(255)
        r = self._dll.deviceGetAllTypes(self._port_b, buf, ctypes.byref(n))   # VERIFY
        if r != 0:
            return                           # keep the configured addresses
        types = buf.raw[: n.value]
        self._types = {addr: t for addr, t in enumerate(types) if t}
        selects = []
        for addr, t in self._types.items():
            if t == TYPE_EXTREME:
                self.extreme_addr = addr
            elif t == TYPE_SELECT_RF:
                self.rf_addr = addr
            elif t == TYPE_SELECT:
                selects.append(addr)
        # lowest address first = NKT's crystal numbering (register 75h)
        self.select_addrs = sorted(selects)

    @staticmethod
    def _check_ch(ch: int) -> None:
        if not 0 <= ch < N_LINES:
            raise ValueError(f"RF channel {ch} out of range 0..{N_LINES - 1}")

    def _read(self, kind, ctype, dev, reg):
        if self._dll is None:
            raise NKTError("not connected")
        v = ctype(0)
        r = getattr(self._dll, "registerRead" + kind)(self._port_b, dev, reg,
                                                      ctypes.byref(v), -1)
        if r != 0:
            raise NKTError(f"read {kind} dev {dev} reg 0x{reg:02X}: result {r}")
        return v.value

    def _write(self, kind, ctype, dev, reg, value):
        if self._dll is None:
            raise NKTError("not connected")
        r = getattr(self._dll, "registerWrite" + kind)(self._port_b, dev, reg,
                                                       ctype(value), -1)
        if r != 0:
            raise NKTError(f"write {kind} dev {dev} reg 0x{reg:02X} = {value}: result {r}")

    def _r8(self, d, r):
        return self._read("U8", ctypes.c_ubyte, d, r)

    def _r16(self, d, r):
        return self._read("U16", ctypes.c_ushort, d, r)

    def _rs16(self, d, r):
        return self._read("S16", ctypes.c_short, d, r)

    def _r32(self, d, r):
        return self._read("U32", ctypes.c_ulong, d, r)

    def _w8(self, d, r, v):
        self._write("U8", ctypes.c_ubyte, d, r, v)

    def _w16(self, d, r, v):
        self._write("U16", ctypes.c_ushort, d, r, v)

    def _w32(self, d, r, v):
        self._write("U32", ctypes.c_ulong, d, r, v)
