"""Which Signal Hound analysers can this PC see? -- for Mission Control's
"Instruments on this PC" (module.toml [hardware] probe = "scripts/probe.py").

THE RULE: a probe only LISTS. It never opens an analyser, never attaches the
tracking generator, never changes a setting. sa_api's
``saGetSerialNumberList(int serials[8], int *count)`` returns the serial
numbers of the SA44B / SA124B units on the USB bus WITHOUT opening them; it is
the only sa_api call made here (saOpenDevice would open one, saGetDeviceType
needs an open handle -- so the MODEL is not known before a service opens it).

The address of a row is the serial number: what ``run_service.py --serial``
takes. The backend claims ``SIGNALHOUND::<serial>`` (backends/sa_api.py
lock_address), which is the row's "lock" -- an analyser the running service
holds shows as held even if the list leaves an open unit out.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

from . import hwlock
from .backends.sa_api import LOCK_MODULE, SaApiAnalyzer, SaApiError, lock_address
from .config import Config

#: saGetSerialNumberList fills an array of at most 8 serials (sa_api.h)
MAX_DEVICES = 8
HELD = "held by the running signalhound service (an open analyser may not be listed)"


def _config() -> Config:
    """signalhound.ini when this PC saved one: it may name hardware.dll_path."""
    ini = Path(__file__).resolve().parents[2] / "signalhound.ini"
    try:
        return Config.load(str(ini)) if ini.is_file() else Config()
    except Exception:
        return Config()


def probe(dll=None) -> dict:
    """{"devices": [...], "note": "..."} -- never raises. `dll` = a fake in tests."""
    devices, notes = [], []
    try:
        # the backend's own DLL search (hardware.dll_path, PATH, Spike folder);
        # only _load / _bind are used -- open() is never called
        sa = SaApiAnalyzer(_config(), dll=dll)
        lib = sa._load()
        sa._bind(lib)
    except SaApiError:
        lib = None
        notes.append("sa_api.dll not found: install Signal Hound Spike (it ships the "
                     "DLL), or set hardware.dll_path in signalhound.ini")
    except Exception as exc:
        lib = None
        notes.append(f"sa_api.dll could not be loaded: {type(exc).__name__}: {exc}")
    if lib is not None:
        try:
            serials = (ctypes.c_int * MAX_DEVICES)()
            count = ctypes.c_int(0)
            st = int(lib.saGetSerialNumberList(serials, ctypes.byref(count)))  # VERIFY
            if st < 0:
                notes.append(f"saGetSerialNumberList: status {st}")
            for i in range(max(0, min(int(count.value), MAX_DEVICES))):
                sn = int(serials[i])
                if sn <= 0:
                    continue
                devices.append({"address": str(sn), "identity": "Signal Hound SA44B / SA124B",
                                "detail": f"serial {sn} (the model is read when a service "
                                          f"opens it)",
                                "lock": lock_address(sn)})
        except Exception as exc:
            notes.append(f"sa_api could not list analysers: {type(exc).__name__}: {exc}")
    seen = {d["address"] for d in devices}
    try:
        for info in hwlock.held():
            a = str(info.get("address", ""))
            if info.get("module") == LOCK_MODULE and a.upper().startswith("SIGNALHOUND::"):
                sn = a.split("::", 1)[1]
                if sn in seen:                   # listed AND held: say so
                    for d in devices:
                        if d["address"] == sn:
                            d["detail"] += "; held by the running signalhound service"
                else:
                    devices.append({"address": sn, "identity": "Signal Hound analyser",
                                    "detail": HELD, "lock": a})
    except Exception:
        pass
    if lib is not None and not devices and not notes:
        notes.append("no Signal Hound analyser listed (USB plugged in? Spike closed?)")
    return {"devices": devices, "note": "; ".join(notes)}
