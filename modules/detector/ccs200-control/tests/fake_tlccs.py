"""A stand-in for TLCCS_64.dll, so the real backend's logic (status polling,
buffers, integration-time caching, error text) is tested without the DLL.

It is called exactly the way ctypes would call the real functions: the
backend passes ctypes objects (byref pointers, arrays), and this fake writes
into them."""

import ctypes as C

import numpy as np

N = 3648


def _deref(ptr, ctype):
    """Write target of a ctypes.byref(...) argument."""
    return C.cast(ptr, C.POINTER(ctype))


class FakeTlccs:
    # Calls that CHANGE the instrument's state. open() must issue none of them
    # (adopt-on-start rule): with forbid_writes=True the fake raises on each.
    WRITES = ("setIntegrationTime",)

    def __init__(self, polls_until_ready=2, fail_init=False, t_int=0.05,
                 forbid_writes=False):
        self.calls = []
        self.polls_until_ready = polls_until_ready
        self.fail_init = fail_init
        self.forbid_writes = forbid_writes
        # the time the unit was LEFT at by its last user: deliberately not the
        # driver default 10 ms, so adoption is visible in the tests
        self.t_int = t_int
        self._polls = 0
        self._scanning = False
        self.level = 0.25

    def tlccs_init(self, rsrc, idq, reset, pvi):
        self.calls.append("init")
        if self.fail_init:
            return -1074001000                       # a negative VISA-style error
        pvi._obj.value = 7
        return 0

    def tlccs_close(self, vi):
        self.calls.append("close")
        return 0

    def tlccs_error_message(self, vi, status, buf):
        msg = b"fake error"
        C.memmove(buf, msg, len(msg))
        return 0

    def tlccs_identificationQuery(self, vi, *bufs):
        for b, text in zip(bufs, (b"Thorlabs", b"CCS200", b"M00000000", b"2.0", b"1.0")):
            C.memmove(b, text, len(text))
        return 0

    def tlccs_setIntegrationTime(self, vi, t):
        self.calls.append("setIntegrationTime")
        if self.forbid_writes:
            raise AssertionError("setIntegrationTime: a state-changing write")
        self.t_int = float(t)
        return 0

    def tlccs_getIntegrationTime(self, vi, pt):
        self.calls.append("getIntegrationTime")
        pt._obj.value = self.t_int
        return 0

    def tlccs_startScan(self, vi):
        self.calls.append("startScan")
        self._scanning, self._polls = True, 0
        return 0

    def tlccs_getDeviceStatus(self, vi, pstatus):
        self._polls += 1
        ready = self._scanning and self._polls > self.polls_until_ready
        pstatus._obj.value = 0x0010 if ready else 0x0004
        return 0

    def tlccs_getScanData(self, vi, buf):
        self.calls.append("getScanData")
        arr = np.ctypeslib.as_array(buf)
        arr[:] = self.level * self.t_int / 0.01
        self._scanning = False
        return 0

    def tlccs_getWavelengthData(self, vi, data_set, buf, plo, phi):
        self.calls.append(f"getWavelengthData({int(data_set)})")
        arr = np.ctypeslib.as_array(buf)
        arr[:] = np.linspace(200.0, 1000.0, N)
        return 0
