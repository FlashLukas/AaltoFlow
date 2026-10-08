"""A fake WaveForms `dwf` library for the dwf backend's tests (no device, no
WaveForms needed). It answers the FDwf* calls the backend makes the way the
SDK documents them -- output arguments by reference, strings into buffers --
and records every call, so a test can prove what was (not) sent.

State at "open" is the lab's AD2 as probed on 2026-10-08: 2 inputs, buffer
16..8192, 0.05 Hz..100 MHz, ranges 5 / 50 V; W1/W2 off; V+/V- off. Attenuation
calls are missing on purpose (an older runtime): the backend must cope.
"""

from __future__ import annotations

import math

import numpy as np

_DONE, _READY, _RUNNING = 2, 0, 3


def _v(x):
    """The Python value of a ctypes argument."""
    return x.value if hasattr(x, "value") else x


def _put(arg, value):
    """Write an output argument (byref(...) or a ctypes object / buffer)."""
    target = getattr(arg, "_obj", arg)
    target.value = value


class FakeDwf:
    def __init__(self, devices=(("SN:FAKE0001", "Analog Discovery 2", False),)):
        self.devices = [list(d) for d in devices]
        self.calls: list[tuple] = []
        self.handle = 0
        self.on_close = None
        self.ain = {"enable": [1, 1], "range": [5.0, 5.0], "offset": [0.0, 0.0],
                    "buffer": 8192, "rate": 100e6, "trig_src": 0, "trig_ch": 0,
                    "level": 0.0, "position": 0.0, "cond": 0, "auto": 0.0,
                    "acquiring": False, "polls": 0}
        base = {"func": 1, "freq": 1000.0, "amp": 1.0, "offset": 0.0, "phase": 0.0,
                "sym": 50.0, "running": False, "enabled": 0, "stopped_by_set": False}
        self.hysteresis = None
        self.autoconfigure = 1
        self.synced_starts = 0
        self.aout = [dict(base), dict(base)]
        self.master = None
        # AnalogIO: (name, label, nodes, units), node values (set / measured)
        self.io = [("V+", "V+", ["Enable", "Voltage", "Current"], ["", "V", "A"]),
                   ("V-", "V-", ["Enable", "Voltage", "Current"], ["", "V", "A"]),
                   ("USB Monitor", "USB", ["Voltage", "Current"], ["V", "A"])]
        self.io_set = {(0, 0): 0.0, (0, 1): 0.0, (1, 0): 0.0, (1, 1): 0.0}
        self.io_master = 0

    def __getattr__(self, name):
        if not name.startswith("FDwf") or "Attenuation" in name:
            raise AttributeError(name)
        impl = getattr(type(self), "_" + name, None)
        if impl is None:
            raise AttributeError(name)

        def call(*args):
            self.calls.append((name,) + tuple(_v(a) if not hasattr(a, "_obj")
                                              and not hasattr(a, "_length_") else "ref"
                                              for a in args))
            r = impl(self, *args)
            return 1 if r is None else r
        return call

    def sent(self, prefix="") -> list:
        """Names of the calls that CHANGE something (…Set, Configure, …)."""
        return [c[0] for c in self.calls
                if c[0].startswith(prefix) and (c[0].endswith("Set")
                                                 or c[0].endswith("Configure"))]

    # ---- enumeration / device --------------------------------------------------------
    def _FDwfEnum(self, flt, n):
        _put(n, len(self.devices))

    def _FDwfEnumSN(self, i, buf):
        buf.value = self.devices[_v(i)][0].encode()

    def _FDwfEnumDeviceName(self, i, buf):
        buf.value = self.devices[_v(i)][1].encode()

    def _FDwfEnumDeviceIsOpened(self, i, busy):
        _put(busy, int(self.devices[_v(i)][2]))

    def _FDwfDeviceOpen(self, i, h):
        self.handle = 1
        _put(h, 1)

    def _FDwfDeviceAutoConfigureSet(self, h, v):
        self.autoconfigure = _v(v)

    def _FDwfDeviceClose(self, h):
        self.handle = 0

    def _FDwfGetLastErrorMsg(self, buf):
        buf.value = b"fake error"

    def _FDwfParamSet(self, p, v):
        if _v(p) == 4:
            self.on_close = _v(v)

    # ---- AnalogIn ------------------------------------------------------------------
    def _FDwfAnalogInChannelCount(self, h, n):
        _put(n, 2)

    def _FDwfAnalogInBufferSizeInfo(self, h, lo, hi):
        _put(lo, 16); _put(hi, 8192)

    def _FDwfAnalogInFrequencyInfo(self, h, lo, hi):
        _put(lo, 0.05); _put(hi, 100e6)

    def _FDwfAnalogInChannelRangeSteps(self, h, arr, n):
        arr[0], arr[1] = 5.0, 50.0
        _put(n, 2)

    def _FDwfAnalogInTriggerSourceGet(self, h, out): _put(out, self.ain["trig_src"])
    def _FDwfAnalogInTriggerChannelGet(self, h, out): _put(out, self.ain["trig_ch"])
    def _FDwfAnalogInTriggerConditionGet(self, h, out): _put(out, self.ain["cond"])
    def _FDwfAnalogInChannelEnableGet(self, h, i, out): _put(out, self.ain["enable"][_v(i)])
    def _FDwfAnalogInChannelRangeGet(self, h, i, out): _put(out, self.ain["range"][_v(i)])
    def _FDwfAnalogInChannelOffsetGet(self, h, i, out): _put(out, self.ain["offset"][_v(i)])
    def _FDwfAnalogInFrequencyGet(self, h, out): _put(out, self.ain["rate"])
    def _FDwfAnalogInBufferSizeGet(self, h, out): _put(out, self.ain["buffer"])
    def _FDwfAnalogInTriggerPositionGet(self, h, out): _put(out, self.ain["position"])
    def _FDwfAnalogInTriggerLevelGet(self, h, out): _put(out, self.ain["level"])

    def _FDwfAnalogInChannelEnableSet(self, h, i, v): self.ain["enable"][_v(i)] = _v(v)

    def _FDwfAnalogInChannelRangeSet(self, h, i, v):
        self.ain["range"][_v(i)] = 50.0 if _v(v) > 5.0 else 5.0

    def _FDwfAnalogInChannelOffsetSet(self, h, i, v): self.ain["offset"][_v(i)] = _v(v)
    def _FDwfAnalogInBufferSizeSet(self, h, v): self.ain["buffer"] = _v(v)
    def _FDwfAnalogInFrequencySet(self, h, v): self.ain["rate"] = _v(v)
    def _FDwfAnalogInTriggerPositionSet(self, h, v): self.ain["position"] = _v(v)
    def _FDwfAnalogInTriggerLevelSet(self, h, v): self.ain["level"] = _v(v)
    def _FDwfAnalogInTriggerSourceSet(self, h, v): self.ain["trig_src"] = _v(v)
    def _FDwfAnalogInTriggerTypeSet(self, h, v): pass
    def _FDwfAnalogInTriggerChannelSet(self, h, v): self.ain["trig_ch"] = _v(v)
    def _FDwfAnalogInTriggerConditionSet(self, h, v): self.ain["cond"] = _v(v)
    def _FDwfAnalogInTriggerAutoTimeoutSet(self, h, v): self.ain["auto"] = _v(v)
    def _FDwfAnalogInTriggerHysteresisSet(self, h, v): self.hysteresis = _v(v)
    def _FDwfAnalogInAcquisitionModeSet(self, h, v): pass

    def _FDwfAnalogInConfigure(self, h, reconf, start):
        self.ain["acquiring"] = bool(_v(start))
        self.ain["polls"] = 0

    def _FDwfAnalogInStatus(self, h, read, state):
        a = self.ain
        if not a["acquiring"]:
            _put(state, _READY)
            return
        a["polls"] += 1
        _put(state, _DONE if a["polls"] >= 2 else _RUNNING)

    def _FDwfAnalogInStatusData(self, h, i, buf, n):
        # each input sees its generator output (the lab's loopback), when on
        o = self.aout[_v(i)]
        n, rate = _v(n), self.ain["rate"]
        t = (np.arange(n) - n / 2) / rate
        y = (o["offset"] + o["amp"] * np.sin(2 * math.pi * o["freq"] * t)
             if o["running"] else np.zeros(n))
        for k in range(n):
            buf[k] = float(y[k])

    # ---- AnalogOut -----------------------------------------------------------------
    def _FDwfAnalogOutCount(self, h, n): _put(n, 2)

    def _FDwfAnalogOutNodeFrequencyInfo(self, h, ch, node, lo, hi):
        _put(lo, 1e-6); _put(hi, 100e6)

    def _FDwfAnalogOutNodeAmplitudeInfo(self, h, ch, node, lo, hi):
        _put(lo, 0.0); _put(hi, 5.0)

    def _FDwfAnalogOutNodeOffsetInfo(self, h, ch, node, lo, hi):
        _put(lo, -5.0); _put(hi, 5.0)

    def _FDwfAnalogOutStatus(self, h, ch, state):
        _put(state, _RUNNING if self.aout[_v(ch)]["running"] else _READY)

    def _FDwfAnalogOutNodeFunctionGet(self, h, ch, node, out): _put(out, self.aout[_v(ch)]["func"])
    def _FDwfAnalogOutNodeFrequencyGet(self, h, ch, node, out): _put(out, self.aout[_v(ch)]["freq"])
    def _FDwfAnalogOutNodeAmplitudeGet(self, h, ch, node, out): _put(out, self.aout[_v(ch)]["amp"])
    def _FDwfAnalogOutNodeOffsetGet(self, h, ch, node, out): _put(out, self.aout[_v(ch)]["offset"])
    def _FDwfAnalogOutNodePhaseGet(self, h, ch, node, out): _put(out, self.aout[_v(ch)]["phase"])
    def _FDwfAnalogOutNodeSymmetryGet(self, h, ch, node, out): _put(out, self.aout[_v(ch)]["sym"])

    def _FDwfAnalogOutNodeEnableSet(self, h, ch, node, v): self.aout[_v(ch)]["enabled"] = _v(v)

    def _node_set(self, ch, key, v):
        self._stop_on_set(ch)
        self.aout[_v(ch)][key] = _v(v)

    def _FDwfAnalogOutNodeFunctionSet(self, h, ch, node, v): self._node_set(ch, "func", v)
    def _FDwfAnalogOutNodeFrequencySet(self, h, ch, node, v): self._node_set(ch, "freq", v)
    def _FDwfAnalogOutNodeAmplitudeSet(self, h, ch, node, v): self._node_set(ch, "amp", v)
    def _FDwfAnalogOutNodeOffsetSet(self, h, ch, node, v): self._node_set(ch, "offset", v)
    def _FDwfAnalogOutNodePhaseSet(self, h, ch, node, v): self._node_set(ch, "phase", v)
    def _FDwfAnalogOutNodeSymmetrySet(self, h, ch, node, v): self._node_set(ch, "sym", v)
    def _FDwfAnalogOutMasterSet(self, h, ch, master): self.master = (_v(ch), _v(master))

    def _FDwfAnalogOutConfigure(self, h, ch, start):
        chans = range(2) if _v(ch) < 0 else [_v(ch)]
        for c in chans:
            if _v(start) == 3:
                # measured on the AD2: "success", and a stopped output stays stopped
                continue
            self.aout[c]["running"] = bool(_v(start))
            self.aout[c]["stopped_by_set"] = False
            if c == 0 and _v(start) and self.master == (1, 0):
                self.aout[1]["running"] = True     # the slave starts with its master
                self.synced_starts += 1

    def _stop_on_set(self, ch):
        """Like the lab's AD2 (2026-10-08): with auto-configure 1 (the
        default) a node parameter set while the output runs STOPS it; with 3
        (dynamic) it keeps running."""
        o = self.aout[_v(ch)]
        if o["running"] and self.autoconfigure != 3:
            o["running"] = False
            o["stopped_by_set"] = True

    # ---- AnalogIO ------------------------------------------------------------------
    def _FDwfAnalogIOStatus(self, h): pass
    def _FDwfAnalogIOChannelCount(self, h, n): _put(n, len(self.io))

    def _FDwfAnalogIOChannelName(self, h, ch, name, label):
        name.value = self.io[_v(ch)][0].encode()
        label.value = self.io[_v(ch)][1].encode()

    def _FDwfAnalogIOChannelInfo(self, h, ch, n): _put(n, len(self.io[_v(ch)][2]))

    def _FDwfAnalogIOChannelNodeName(self, h, ch, nd, name, units):
        name.value = self.io[_v(ch)][2][_v(nd)].encode()
        units.value = self.io[_v(ch)][3][_v(nd)].encode()

    def _FDwfAnalogIOChannelNodeGet(self, h, ch, nd, out):
        _put(out, self.io_set.get((_v(ch), _v(nd)), 0.0))

    def _FDwfAnalogIOChannelNodeStatus(self, h, ch, nd, out):
        ch, nd = _v(ch), _v(nd)
        if ch == 2:
            _put(out, 5.02 if nd == 0 else 0.31)
            return
        on = self.io_set[(ch, 0)] and self.io_master
        node = self.io[ch][2][nd]
        _put(out, (self.io_set[(ch, 1)] if on else 0.0) if node == "Voltage" else 0.001)

    def _FDwfAnalogIOChannelNodeSet(self, h, ch, nd, v): self.io_set[(_v(ch), _v(nd))] = _v(v)
    def _FDwfAnalogIOEnableGet(self, h, out): _put(out, self.io_master)
    def _FDwfAnalogIOEnableSet(self, h, v): self.io_master = _v(v)

    def _FDwfAnalogIOChannelNodeInfo(self, h, ch, nd, lo, hi, steps):
        _put(lo, 0.0 if _v(ch) == 0 else -5.0)
        _put(hi, 5.0 if _v(ch) == 0 else 0.0)
        _put(steps, 501)
