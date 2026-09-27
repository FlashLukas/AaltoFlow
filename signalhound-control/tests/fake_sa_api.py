"""A fake sa_api.dll: the functions the real backend calls, with the same
argument conventions (handles as ints, results written through ctypes
pointers), so SaApiAnalyzer can be tested with no Signal Hound attached.

It records every call, which lets a test check the ORDER of the configuration
calls against the manual, and that close() aborts before closing (TG off).
"""

from __future__ import annotations

import numpy as np


class _Fn:
    """A callable that tolerates `restype` / `argtypes` being set on it, like
    a ctypes function pointer does."""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self._fn(*args)


def _set(ptr, value):
    """Write through a ctypes.byref() argument."""
    ptr._obj.value = value


class FakeSaApi:
    def __init__(self, device_type=2, serial=17040001, tg=True, bins=None,
                 compression=False, fail=None):
        self.device_type = device_type        # 2 = SA44B, 4 = SA124B
        self.serial = serial
        self.tg = tg
        self.bins = bins                      # force a bin count (else from span / RBW)
        self.compression = compression
        self.fail = fail or {}                # name -> status code to return
        self.calls: list[tuple] = []
        self.open = False
        self.mode = -1                        # SA_IDLE
        self.tg_attached = False
        self.center = self.span = self.rbw = 0.0
        self.tg_level = None
        self.tg_configured = False            # saConfigTgSweep called since the last abort
        for name in ("saOpenDevice", "saOpenDeviceBySerialNumber", "saCloseDevice",
                     "saGetSerialNumber", "saGetDeviceType", "saConfigAcquisition",
                     "saConfigCenterSpan", "saConfigLevel", "saConfigGainAtten",
                     "saConfigSweepCoupling", "saInitiate", "saAbort", "saQuerySweepInfo",
                     "saGetSweep_32f", "saAttachTg", "saIsTgAttached", "saConfigTgSweep",
                     "saStoreTgThru", "saSetTg", "saGetErrorString", "saGetAPIVersion"):
            setattr(self, name, _Fn(self._wrap(name, getattr(self, "_" + name))))

    def _wrap(self, name, fn):
        def call(*args):
            self.calls.append((name,) + tuple(a if isinstance(a, (int, float, bool, str))
                                              else "<ptr>" for a in args))
            if name in self.fail:
                return self.fail[name]
            return fn(*args)
        return call

    def names(self):
        return [c[0] for c in self.calls]

    # ---- the API ----------------------------------------------------------
    def _saOpenDevice(self, h):
        _set(h, 0); self.open = True; return 0

    def _saOpenDeviceBySerialNumber(self, h, serial):
        if serial != self.serial:
            return -8                          # saDeviceNotFoundErr
        _set(h, 0); self.open = True; return 0

    def _saCloseDevice(self, h):
        self.open = False; self.mode = -1; return 0

    def _saGetSerialNumber(self, h, p):
        _set(p, self.serial); return 0

    def _saGetDeviceType(self, h, p):
        _set(p, self.device_type); return 0

    def _saConfigAcquisition(self, h, det, scale):
        self.detector = det; return 0

    def _saConfigCenterSpan(self, h, c, s):
        self.center, self.span = c, s; return 0

    def _saConfigLevel(self, h, ref):
        self.ref = ref; return 0

    def _saConfigGainAtten(self, h, atten, gain, pre):
        return 0

    def _saConfigSweepCoupling(self, h, rbw, vbw, reject):
        self.rbw = rbw; return 0

    def _saInitiate(self, h, mode, flag):
        self.mode = mode; return 0

    def _saAbort(self, h):
        self.mode = -1; self.tg_configured = False; return 0

    def _n(self):
        if self.bins:
            return int(self.bins)
        if self.mode == 4:
            return int(self.tg_points)
        return int(self.span / (self.rbw / 2)) + 1

    def _saQuerySweepInfo(self, h, n, start, step):
        n_ = self._n()
        _set(n, n_)
        _set(start, self.center - self.span / 2)
        _set(step, self.span / (n_ - 1))
        return 0

    def _saGetSweep_32f(self, h, mn, mx):
        n = self._n()
        a = np.ctypeslib.as_array(mn, shape=(n,))
        b = np.ctypeslib.as_array(mx, shape=(n,))
        a[:] = -90.0
        b[:] = -85.0
        a[n // 2] = b[n // 2] = -20.0          # one tone in the middle
        return 2 if self.compression else 0

    def _saAttachTg(self, h):
        if not self.tg:
            return -10                         # saTrackingGeneratorNotFound
        self.tg_attached = True; return 0

    def _saIsTgAttached(self, h, p):
        _set(p, self.tg_attached); return 0

    def _saConfigTgSweep(self, h, size, hdr, passive):
        self.tg_points = size; self.tg_configured = True; return 0

    def _saStoreTgThru(self, h, flag):
        return 0

    def _saSetTg(self, h, f, amp):
        # sa_api.h: only allowed while the TG is NOT configured and initiated
        # for TG sweeps. The real DLL's error code for it is not documented;
        # saDeviceNotIdleErr (-9) is the closest.
        if self.tg_configured or self.mode == 4:
            return -9
        self.tg_level = amp; return 0

    def _saGetErrorString(self, code):
        return f"fake error {code}".encode("ascii")

    def _saGetAPIVersion(self):
        return b"3.0.99"

