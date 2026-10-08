"""The real Digilent Analog Discovery (2 or 3), through the WaveForms runtime.

The ONLY file that touches the vendor library: `dwf.dll` (Windows; libdwf.so on
Linux, the dwf framework on macOS), loaded with ctypes LAZILY inside open(), so
the package imports and the simulator runs on a PC without WaveForms. Written
for the dwf API in general (FDwfAnalogIn* / FDwfAnalogOut* / FDwfAnalogIO*,
WaveForms SDK reference manual), with the numbers -- channels, buffer, ranges,
rates, supplies -- READ from the device, so an AD3 works too.

ONE DEVICE, THREE PARTS, ONE SERVICE. The scope (AnalogIn), the generator
(AnalogOut, W1/W2) and the power supplies (AnalogIO, V+/V-) are one USB
device that only one process can open. `DwfDevice` owns that connection; the
scope backend (`DwfScope`, ScopeBackend) and the generator backend
(`DwfWaveGen`, the generator brain's WaveGen) share it. It is opened by the
first of them and closed by the last (a reference count), and every call goes
through ONE lock -- the scope's trace thread and the generator's worker are
different threads, and the dwf handle is not guaranteed thread-safe.

The address is claimed with hwlock as "dwf:<serial>" (one instrument, one
service). Measured on the lab's AD2 (2026-10-08, read-only probe): WaveForms
3.24.4; scope 2 ch, 14 bit, buffer 16..8192, 0.05 Hz..100 MHz, ranges 5 and
50 V; generator W1/W2, 1 uHz..100 MHz, amplitude 0.01..5 V, both disabled at
open; AnalogIO V+ / V- supplies, USB monitor, aux monitor.

ADOPT AT START: open() only queries. The scope part arms an acquisition when
the brain first asks for a trace (reading the inputs changes nothing outside
the device); the generator outputs and the supplies are left as found (off,
on the lab's AD2 -- opening the device resets it).

UNITS / CONVENTIONS mapped to the scope brain's (which came from the Siglent):
  * V/div = range / 8 (the brain's screen is +-4 divisions; a dwf range is the
    whole input span, peak-to-peak);
  * the brain's offset is ADDED to the signal before the screen window (the
    Siglent's OFST): the dwf offset is the voltage at the CENTRE of the range,
    so dwf offset = -offset_V;
  * time/div = record / 10 divisions; record = buffer / rate. The buffer is
    the device's largest (8192 on an AD2), the rate follows the time/div;
  * delay = the trigger position (FDwfAnalogInTriggerPositionSet, seconds;
    0 = the trigger in the middle of the record);
  * trigger mode: auto = an auto-timeout (free-runs when nothing triggers),
    normal = wait for the trigger, single = one record then stop, stop = no
    acquisitions. The device has no run mode of its own while idle: the
    module's mode decides whether it re-arms.

Every call not yet confirmed on the lab's AD2 is marked # VERIFY.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time

import numpy as np

from ..hwlock import claim

# ---- dwf constants (dwf.h, WaveForms SDK) -------------------------------------------
_ENUM_ALL = 0
_ACQMODE_SINGLE = 0
_TRIGSRC_NONE = 0
_TRIGSRC_DETECTOR_ANALOG_IN = 2
_TRIGSRC_ANALOG_OUT1 = 7                 # W1 starting (out2 = 8) # VERIFY numbers
_TRIGSRC_EXTERNAL1 = 11                  # T1 (external2 = 12)
_TRIGTYPE_EDGE = 0
_SLOPE_RISE, _SLOPE_FALL = 0, 1
_STATE_DONE = 2
_NODE_CARRIER = 0
_FUNC = {"dc": 0, "sine": 1, "square": 2, "ramp": 3, "noise": 6, "pulse": 7}
_FUNC_NAMES = {0: "dc", 1: "sine", 2: "square", 3: "ramp", 4: "ramp", 5: "ramp",
               6: "noise", 7: "pulse"}     # 4/5 = ramp up/down: read as a ramp
_PARAM_ON_CLOSE = 4                      # DwfParamOnClose: 0 run, 1 stop, 2 shutdown # VERIFY

_SOURCES = {"ch1": (_TRIGSRC_DETECTOR_ANALOG_IN, 0), "ch2": (_TRIGSRC_DETECTOR_ANALOG_IN, 1),
            "ext1": (_TRIGSRC_EXTERNAL1, None), "ext2": (_TRIGSRC_EXTERNAL1 + 1, None),
            "w1": (_TRIGSRC_ANALOG_OUT1, None), "w2": (_TRIGSRC_ANALOG_OUT1 + 1, None)}
# which trigger settings mean something for which source: a channel has a
# level and a slope; a digital pin (T1/T2) a slope; a generator start neither
TRIGGER_OPTIONS = {"ch1": {"level": True, "slope": True}, "ch2": {"level": True, "slope": True},
                   "ext1": {"level": False, "slope": True}, "ext2": {"level": False, "slope": True},
                   "w1": {"level": False, "slope": False}, "w2": {"level": False, "slope": False}}
_AUTO_TIMEOUT_S = 0.2                    # auto mode: free-run after this long without a trigger


class DwfError(RuntimeError):
    """A dwf call returned false; the text is the library's own error message."""


def _load_library():
    """The WaveForms runtime's library. Lazily: only a real-hardware open()
    gets here."""
    if sys.platform.startswith("win"):
        return ctypes.cdll.dwf
    if sys.platform == "darwin":
        return ctypes.cdll.LoadLibrary("/Library/Frameworks/dwf.framework/dwf")
    return ctypes.cdll.LoadLibrary("libdwf.so")


class DwfDevice:
    """The shared connection to one Analog Discovery (see module docstring)."""

    def __init__(self, device: str = ""):
        self.device = device or ""
        self.lock = threading.RLock()
        self._dll = None
        self._h = ctypes.c_int(0)
        self._users = 0
        self._hwlock = None
        self.name = ""
        self.serial = ""
        self.keep_running = False        # set by a keep_outputs shutdown before close

    # ---- calls ------------------------------------------------------------------
    def call(self, fn: str, *args) -> None:
        """One dwf call with the device handle first; raises DwfError on false."""
        with self.lock:
            ok = getattr(self._dll, fn)(self._h, *args)
            if not ok:
                raise DwfError(f"{fn}: {self.last_error()}")

    def call_raw(self, fn: str, *args) -> int:
        """A dwf call WITHOUT the handle (enumeration)."""
        with self.lock:
            return getattr(self._dll, fn)(*args)

    def has(self, fn: str) -> bool:
        """Does this runtime have `fn`? (newer calls, e.g. attenuation)"""
        try:
            getattr(self._dll, fn)
            return True
        except AttributeError:
            return False

    def last_error(self) -> str:
        buf = ctypes.create_string_buffer(512)
        try:
            self._dll.FDwfGetLastErrorMsg(buf)
        except Exception:
            return "unknown error"
        return buf.value.decode("ascii", "replace").strip() or "unknown error"

    def get_double(self, fn: str, *args) -> float:
        v = ctypes.c_double()
        self.call(fn, *args, ctypes.byref(v))
        return v.value

    def get_int(self, fn: str, *args) -> int:
        v = ctypes.c_int()
        self.call(fn, *args, ctypes.byref(v))
        return v.value

    # ---- lifecycle --------------------------------------------------------------
    def open(self) -> None:
        with self.lock:
            self._users += 1
            if self._users > 1:
                return
            try:
                self._open()
            except BaseException:
                self._users = 0
                self._release()
                raise

    def _open(self) -> None:
        self._dll = _load_library()
        n = ctypes.c_int()
        self._dll.FDwfEnum(ctypes.c_int(_ENUM_ALL), ctypes.byref(n))
        found = []
        for i in range(n.value):
            sn = ctypes.create_string_buffer(32)
            name = ctypes.create_string_buffer(32)
            busy = ctypes.c_int()
            self._dll.FDwfEnumSN(ctypes.c_int(i), sn)
            self._dll.FDwfEnumDeviceName(ctypes.c_int(i), name)
            self._dll.FDwfEnumDeviceIsOpened(ctypes.c_int(i), ctypes.byref(busy))
            found.append((i, sn.value.decode("ascii", "replace"),
                          name.value.decode("ascii", "replace"), bool(busy.value)))
        if not found:
            raise DwfError("no Analog Discovery found (is it plugged in, and WaveForms "
                           "installed?)")
        want = self.device.strip()
        if want.startswith("#"):
            pick = [d for d in found if d[0] == int(want[1:])]
        elif want:
            pick = [d for d in found if d[1].upper().endswith(want.upper())]
        else:
            pick = [d for d in found if not d[3]] or found
        if not pick:
            raise DwfError(f"no Analog Discovery {want!r} (found {len(found)})")
        idx, self.serial, self.name, _busy = pick[0]
        # one instrument, one service: claim BEFORE opening it
        self._hwlock = claim(f"dwf:{self.serial or idx}", "scope")
        h = ctypes.c_int(0)
        self._dll.FDwfDeviceOpen(ctypes.c_int(idx), ctypes.byref(h))
        if h.value == 0:
            raise DwfError(f"cannot open {self.name}: {self.last_error()} (WaveForms "
                           f"itself, or another program, may have it open)")
        self._h = h

    def close(self) -> None:
        with self.lock:
            if self._users == 0:
                return
            self._users -= 1
            if self._users > 0:
                return
            self._release()

    def _release(self) -> None:
        try:
            if self._dll is not None and self._h.value:
                if self.keep_running:
                    # a RESTART (keep_outputs): ask the device to keep running
                    # when the handle closes (per device where the runtime has
                    # it, else the global parameter). MEASURED on the lab AD2
                    # (2026-10-08): the next start found W1 OFF -- opening the
                    # device resets it, so on the AD2 a restart cannot keep the
                    # outputs whatever happens here (README).        # VERIFY
                    try:
                        if hasattr(self._dll, "FDwfDeviceParamSet"):
                            self._dll.FDwfDeviceParamSet(self._h, ctypes.c_int(_PARAM_ON_CLOSE),
                                                         ctypes.c_int(0))
                        else:
                            self._dll.FDwfParamSet(ctypes.c_int(_PARAM_ON_CLOSE), ctypes.c_int(0))
                    except Exception:
                        pass
                self._dll.FDwfDeviceClose(self._h)
        finally:
            self._h = ctypes.c_int(0)
            if self._hwlock is not None:
                self._hwlock.release()
                self._hwlock = None

    @property
    def is_open(self) -> bool:
        return bool(self._h.value)

    def idn(self) -> str:
        # the serial is shown at runtime (status) but never written to a file
        return f"Digilent,{self.name},{self.serial}" if self.is_open else ""

    # ---- the power supplies (AnalogIO) ------------------------------------------------
    def _io_channels(self) -> list:
        """[(index, name, label, [node names], [node units])] of AnalogIO."""
        out = []
        for ch in range(self.get_int("FDwfAnalogIOChannelCount")):
            name = ctypes.create_string_buffer(32)
            label = ctypes.create_string_buffer(16)
            self.call("FDwfAnalogIOChannelName", ctypes.c_int(ch), name, label)
            nodes, units = [], []
            for nd in range(self.get_int("FDwfAnalogIOChannelInfo", ctypes.c_int(ch))):
                nn = ctypes.create_string_buffer(32)
                uu = ctypes.create_string_buffer(16)
                self.call("FDwfAnalogIOChannelNodeName", ctypes.c_int(ch), ctypes.c_int(nd),
                          nn, uu)
                nodes.append(nn.value.decode("ascii", "replace"))
                units.append(uu.value.decode("ascii", "replace"))
            out.append((ch, name.value.decode("ascii", "replace"),
                        label.value.decode("ascii", "replace"), nodes, units))
        return out


# ======================================================================================
# the scope
# ======================================================================================

class DwfScope:
    """ScopeBackend for an Analog Discovery's oscilloscope (AnalogIn)."""

    simulated = False
    keeps_outputs_on_close = True          # the brain passes keep_outputs to close()
    # measured on the lab AD2 (2026-10-08): opening the device resets it, so
    # outputs / supplies do NOT survive a restart (keep_outputs) -- said at shutdown
    resets_on_open = True

    def __init__(self, device: DwfDevice, hysteresis_div: float = 0.05):
        self.dev = device
        # trigger hysteresis as a fraction of the source's V/div (lab AD2:
        # without it the 4.7 mV input noise made rising crossings on the
        # FALLING edge -- the slope was ignored and averages cancelled out)
        self.hysteresis_div = float(hysteresis_div)
        self._n_ch = 2
        self._buf_max = 8192
        self._f_range = (0.05, 100e6)
        self._ranges: list[float] = [5.0, 50.0]
        self._armed = False
        self._mode = "auto"                 # the module's run mode (see docstring)
        self._source = "ch1"
        self._slope = "rising"
        self._record = None
        self._io = None                    # AnalogIO channel map (supplies)
        self.open_warnings: list[str] = []

    # ---- lifecycle ------------------------------------------------------------------
    def open(self) -> None:
        d = self.dev
        d.open()
        try:
            self._n_ch = d.get_int("FDwfAnalogInChannelCount")
            lo, hi = ctypes.c_int(), ctypes.c_int()
            d.call("FDwfAnalogInBufferSizeInfo", ctypes.byref(lo), ctypes.byref(hi))
            self._buf_max = hi.value
            flo, fhi = ctypes.c_double(), ctypes.c_double()
            d.call("FDwfAnalogInFrequencyInfo", ctypes.byref(flo), ctypes.byref(fhi))
            self._f_range = (flo.value, fhi.value)
            steps = (ctypes.c_double * 32)()
            n = ctypes.c_int()
            d.call("FDwfAnalogInChannelRangeSteps", steps, ctypes.byref(n))     # VERIFY
            self._ranges = sorted({float(steps[i]) for i in range(n.value)}) or [5.0, 50.0]
            # the trigger the device holds -> the module's source / slope
            src = d.get_int("FDwfAnalogInTriggerSourceGet")                     # VERIFY
            if src == _TRIGSRC_DETECTOR_ANALOG_IN:
                self._source = "ch2" if d.get_int("FDwfAnalogInTriggerChannelGet") == 1 else "ch1"
            else:
                self._source = next((k for k, (s, _c) in _SOURCES.items()
                                     if s == src and _c is None), "ch1")
            try:
                self._slope = ("falling" if d.get_int("FDwfAnalogInTriggerConditionGet")
                               == _SLOPE_FALL else "rising")                    # VERIFY
            except DwfError:
                pass
        except BaseException:
            d.close()
            raise

    def close(self, keep_outputs: bool = False) -> None:
        if self.dev.is_open and self._armed:
            try:
                self.dev.call("FDwfAnalogInConfigure", ctypes.c_int(0), ctypes.c_int(0))
            except Exception:
                pass
        self._armed = False
        self.dev.keep_running = bool(keep_outputs)
        self.dev.close()

    def capabilities(self) -> dict:
        return {"model": self.dev.name or "Analog Discovery",
                "channels": ["ch1", "ch2"][:max(1, min(2, self._n_ch))],
                "ext_trigger": True, "generator_channels": 2,
                "max_points": self._buf_max,
                "couplings": ["dc"],
                "rolls": False,
                "trigger_sources": list(_SOURCES),
                "trigger_options": TRIGGER_OPTIONS,
                "supplies": True}

    def idn(self) -> str:
        return self.dev.idn()

    # ---- settings -------------------------------------------------------------------
    def _rate(self) -> float:
        return self.dev.get_double("FDwfAnalogInFrequencyGet")

    def _buffer(self) -> int:
        return self.dev.get_int("FDwfAnalogInBufferSizeGet")

    def read_settings(self) -> dict:
        d = self.dev
        out = {"channels": {}, "unread": []}
        for i, ch in enumerate(("ch1", "ch2")[:self._n_ch]):
            c = {"coupling": "dc"}
            try:
                c["enabled"] = bool(d.get_int("FDwfAnalogInChannelEnableGet", ctypes.c_int(i)))
                c["vdiv_V"] = d.get_double("FDwfAnalogInChannelRangeGet", ctypes.c_int(i)) / 8.0
                c["offset_V"] = -d.get_double("FDwfAnalogInChannelOffsetGet", ctypes.c_int(i))
                c["probe"] = (d.get_double("FDwfAnalogInChannelAttenuationGet", ctypes.c_int(i))
                              if d.has("FDwfAnalogInChannelAttenuationGet") else 1.0)
            except DwfError:
                out["unread"].append(ch)
            out["channels"][ch] = c
        try:
            rate, n = self._rate(), self._buffer()
            out["sample_rate_Hz"] = rate
            out["record_points"] = n
            out["tdiv_s"] = n / rate / 10.0
            out["delay_s"] = d.get_double("FDwfAnalogInTriggerPositionGet")     # VERIFY sign
        except DwfError:
            out["unread"].append("timebase")
        trg = {"source": self._source, "slope": self._slope, "mode": self._mode}
        try:
            trg["level_V"] = d.get_double("FDwfAnalogInTriggerLevelGet")
        except DwfError:
            trg["level_V"] = None
            out["unread"].append("trigger.level_V")
        out["trigger"] = trg
        return out

    def set_channel(self, ch: str, **values) -> None:
        d, i = self.dev, ctypes.c_int(0 if ch == "ch1" else 1)
        if values.get("coupling", "dc") != "dc":
            raise ValueError("the Analog Discovery's inputs are DC-coupled only")
        if "enabled" in values:
            d.call("FDwfAnalogInChannelEnableSet", i, ctypes.c_int(int(bool(values["enabled"]))))
        if "probe" in values and d.has("FDwfAnalogInChannelAttenuationSet"):
            d.call("FDwfAnalogInChannelAttenuationSet", i, ctypes.c_double(float(values["probe"])))
        if "vdiv_V" in values:
            # the smallest range that holds 8 divisions (AD2: 5 V or 50 V)
            want = 8.0 * float(values["vdiv_V"])
            rng = next((r for r in self._ranges if r >= want * 0.999), self._ranges[-1])
            d.call("FDwfAnalogInChannelRangeSet", i, ctypes.c_double(rng))
        if "offset_V" in values:
            d.call("FDwfAnalogInChannelOffsetSet", i, ctypes.c_double(-float(values["offset_V"])))
        self._rearm()

    def set_timebase(self, tdiv_s=None, delay_s=None) -> None:
        d = self.dev
        if tdiv_s is not None:
            n = self._buf_max
            d.call("FDwfAnalogInBufferSizeSet", ctypes.c_int(n))
            rate = min(max(n / (10.0 * float(tdiv_s)), self._f_range[0]), self._f_range[1])
            d.call("FDwfAnalogInFrequencySet", ctypes.c_double(rate))
        if delay_s is not None:
            d.call("FDwfAnalogInTriggerPositionSet", ctypes.c_double(float(delay_s)))  # VERIFY
        self._rearm()

    def set_trigger(self, **values) -> None:
        d = self.dev
        if "source" in values:
            src = values["source"]
            if src not in _SOURCES:
                raise ValueError(f"the Analog Discovery has no trigger source {src!r}")
            self._source = src
        if "slope" in values:
            self._slope = values["slope"]
        if "mode" in values:
            self._mode = values["mode"]
        if "level_V" in values and values["level_V"] is not None:
            d.call("FDwfAnalogInTriggerLevelSet", ctypes.c_double(float(values["level_V"])))
        self._apply_trigger()
        self._rearm()

    def _apply_trigger(self) -> None:
        d = self.dev
        src, chan = _SOURCES[self._source]
        d.call("FDwfAnalogInTriggerSourceSet", ctypes.c_ubyte(src))                  # VERIFY type
        if chan is not None:
            d.call("FDwfAnalogInTriggerTypeSet", ctypes.c_int(_TRIGTYPE_EDGE))
            d.call("FDwfAnalogInTriggerChannelSet", ctypes.c_int(chan))
            if d.has("FDwfAnalogInTriggerHysteresisSet"):
                vdiv = d.get_double("FDwfAnalogInChannelRangeGet", ctypes.c_int(chan)) / 8.0
                d.call("FDwfAnalogInTriggerHysteresisSet",
                       ctypes.c_double(self.hysteresis_div * vdiv))
        d.call("FDwfAnalogInTriggerConditionSet",
               ctypes.c_int(_SLOPE_FALL if self._slope == "falling" else _SLOPE_RISE))  # VERIFY
        d.call("FDwfAnalogInTriggerAutoTimeoutSet",
               ctypes.c_double(_AUTO_TIMEOUT_S if self._mode == "auto" else 0.0))

    # ---- traces ---------------------------------------------------------------------
    def _arm(self) -> None:
        d = self.dev
        d.call("FDwfAnalogInAcquisitionModeSet", ctypes.c_int(_ACQMODE_SINGLE))
        if self._buffer() != self._buf_max:
            d.call("FDwfAnalogInBufferSizeSet", ctypes.c_int(self._buf_max))
        self._apply_trigger()
        d.call("FDwfAnalogInConfigure", ctypes.c_int(1), ctypes.c_int(1))
        self._armed = True

    def _rearm(self) -> None:
        if self._armed and self._mode != "stop":
            self.dev.call("FDwfAnalogInConfigure", ctypes.c_int(1), ctypes.c_int(1))
        elif self._mode == "stop" and self._armed:
            self.dev.call("FDwfAnalogInConfigure", ctypes.c_int(0), ctypes.c_int(0))
            self._armed = False

    def new_trace_ready(self) -> bool:
        """Arms the first acquisition (the inputs only), then: True once one
        has finished -- its data are copied out at once and the next one is
        armed (unless single / stop)."""
        if self._mode == "stop":
            return False
        d = self.dev
        if not self._armed:
            self._arm()
            return False
        sts = ctypes.c_ubyte()
        d.call("FDwfAnalogInStatus", ctypes.c_int(1), ctypes.byref(sts))
        if sts.value != _STATE_DONE:
            return False
        n = self._buffer()
        rate = self._rate()
        pos = d.get_double("FDwfAnalogInTriggerPositionGet")
        data = {}
        for i, ch in enumerate(("ch1", "ch2")[:self._n_ch]):
            buf = (ctypes.c_double * n)()
            d.call("FDwfAnalogInStatusData", ctypes.c_int(i), buf, ctypes.c_int(n))
            data[ch] = np.frombuffer(buf, dtype=np.float64).copy()
        # the trigger in the middle of the buffer, moved by the position # VERIFY
        t = pos + (np.arange(n) - n / 2.0) / rate
        self._record = (t, data)
        if self._mode == "single":
            self._mode = "stop"
            self._armed = False
        else:
            d.call("FDwfAnalogInConfigure", ctypes.c_int(0), ctypes.c_int(1))
        return True

    def read_traces(self, channels, max_points):
        if self._record is None:
            raise RuntimeError("no record yet")
        t, data = self._record
        step = max(1, int(np.ceil(t.size / max(1, int(max_points)))))
        return t[::step].copy(), {ch: data[ch][::step].copy() for ch in channels}

    # ---- power supplies (V+ / V-) -----------------------------------------------------
    def _supply_map(self) -> dict:
        """{"vplus": (channel, {node name: index}), "vminus": ...} by NAME, so
        an AD3 with other channel numbers works too."""
        if self._io is None:
            io = {}
            for ch, name, label, nodes, _units in self.dev._io_channels():
                key = ("vplus" if "+" in name or "+" in label else
                       "vminus" if "-" in name or "-" in label else None)
                if key and key not in io and "Enable" in " ".join(nodes):
                    io[key] = (ch, {n: k for k, n in enumerate(nodes)})
            self._io = io
        return self._io

    def read_supplies(self) -> dict:
        """{"vplus": {"on", "V", "V_meas", "A_meas"}, "vminus": {...},
            "monitors": {"USB Monitor Voltage V": 5.01, ...}} -- queries only."""
        d = self.dev
        d.call("FDwfAnalogIOStatus")
        out = {"monitors": {}}
        smap = self._supply_map()
        for key, (ch, nodes) in smap.items():
            s = {}
            for node, field in (("Enable", "on"), ("Voltage", "V")):
                if node in nodes:
                    v = ctypes.c_double()
                    d.call("FDwfAnalogIOChannelNodeGet", ctypes.c_int(ch),
                           ctypes.c_int(nodes[node]), ctypes.byref(v))
                    s[field] = bool(v.value) if field == "on" else v.value
            for node, field in (("Voltage", "V_meas"), ("Current", "A_meas")):
                if node in nodes:
                    v = ctypes.c_double()
                    try:
                        d.call("FDwfAnalogIOChannelNodeStatus", ctypes.c_int(ch),
                               ctypes.c_int(nodes[node]), ctypes.byref(v))       # VERIFY
                        s[field] = v.value
                    except DwfError:
                        pass
            out[key] = s
        for ch, name, label, nodes, units in self.dev._io_channels():
            if any(ch == c for c, _n in smap.values()):
                continue
            for nd, (nn, uu) in enumerate(zip(nodes, units)):
                v = ctypes.c_double()
                try:
                    d.call("FDwfAnalogIOChannelNodeStatus", ctypes.c_int(ch),
                           ctypes.c_int(nd), ctypes.byref(v))
                except DwfError:
                    continue
                out["monitors"][f"{name} {nn} {uu}".strip()] = v.value
        try:
            out["master_on"] = bool(d.get_int("FDwfAnalogIOEnableGet"))         # VERIFY
        except DwfError:
            pass
        return out

    def set_supply(self, which: str, on: bool | None = None, volts: float | None = None) -> None:
        d = self.dev
        smap = self._supply_map()
        if which not in smap:
            raise ValueError(f"this device has no {which} supply")
        ch, nodes = smap[which]
        if volts is not None and "Voltage" in nodes:
            d.call("FDwfAnalogIOChannelNodeSet", ctypes.c_int(ch),
                   ctypes.c_int(nodes["Voltage"]), ctypes.c_double(float(volts)))
        if on is not None:
            d.call("FDwfAnalogIOChannelNodeSet", ctypes.c_int(ch),
                   ctypes.c_int(nodes["Enable"]), ctypes.c_double(1.0 if on else 0.0))
            if on:
                # the AD2's supplies also need the master switch      # VERIFY
                d.call("FDwfAnalogIOEnableSet", ctypes.c_int(1))

    def supply_range(self, which: str) -> tuple[float, float]:
        """The device's own range of a supply's voltage node."""
        smap = self._supply_map()
        if which not in smap or "Voltage" not in smap[which][1]:
            return (0.0, 0.0)
        ch, nodes = smap[which]
        lo, hi, steps = ctypes.c_double(), ctypes.c_double(), ctypes.c_int()
        self.dev.call("FDwfAnalogIOChannelNodeInfo", ctypes.c_int(ch),
                      ctypes.c_int(nodes["Voltage"]), ctypes.byref(lo), ctypes.byref(hi),
                      ctypes.byref(steps))                                      # VERIFY
        return (lo.value, hi.value)


# ======================================================================================
# the generator (W1 / W2)
# ======================================================================================

class DwfWaveGen:
    """WaveGen (generator/base.py) for an Analog Discovery's W1/W2 (AnalogOut).

    DWF AMPLITUDE IS THE PEAK (half the peak-to-peak) -- # VERIFY on the AD2
    with the scope: 1 Vpp asked must read 1 Vpp. Duty (pulse / square) and
    ramp symmetry are the same dwf node parameter (symmetry, %)."""

    simulated = False

    def __init__(self, device: DwfDevice):
        self.dev = device
        self._n = 2
        self._info: list[dict] = []
        self._shape_extra: dict = {}            # (ch) -> {"duty_pct", "symmetry_pct"} last set
        self._on: dict = {}                      # (ch) -> running, as last read / set

    def open(self) -> None:
        d = self.dev
        d.open()
        try:
            self._n = min(2, d.get_int("FDwfAnalogOutCount"))
            self._info = []
            for ch in range(self._n):
                c = ctypes.c_int(ch)
                node = ctypes.c_int(_NODE_CARRIER)
                f0, f1 = ctypes.c_double(), ctypes.c_double()
                a0, a1 = ctypes.c_double(), ctypes.c_double()
                o0, o1 = ctypes.c_double(), ctypes.c_double()
                d.call("FDwfAnalogOutNodeFrequencyInfo", c, node, ctypes.byref(f0), ctypes.byref(f1))
                d.call("FDwfAnalogOutNodeAmplitudeInfo", c, node, ctypes.byref(a0), ctypes.byref(a1))
                d.call("FDwfAnalogOutNodeOffsetInfo", c, node, ctypes.byref(o0), ctypes.byref(o1))
                self._info.append({"freq_min_Hz": max(f0.value, 1e-6), "freq_max_Hz": f1.value,
                                   "amp_min_Vpp": 2 * a0.value, "amp_max_Vpp": 2 * a1.value,
                                   "peak_max_V": max(abs(o0.value), abs(o1.value))})
        except BaseException:
            d.close()
            raise

    def close(self, outputs_off: bool = True) -> None:
        if self.dev.is_open and outputs_off:
            for ch in range(self._n):
                try:
                    self.set_output(ch, False)
                except Exception:
                    pass
        self.dev.keep_running = self.dev.keep_running or not outputs_off
        self.dev.close()

    def capabilities(self) -> dict:
        return {"model": f"{self.dev.name or 'Analog Discovery'} generator",
                "channels": self._n,
                "waveforms": ["sine", "square", "pulse", "ramp", "noise", "dc"],
                "phase_align": True, "phase_resolution_deg": 0.0,
                "load_settable": False, "ramp_symmetry": True}

    def envelope(self, waveform: str, load_ohm=None) -> dict:
        from ..generator.sim import ad_envelope
        info = dict(self._info[0]) if self._info else None
        if info:
            # the analog bandwidth, not the rate the driver accepts (100 MHz)
            info["freq_max_Hz"] = min(info["freq_max_Hz"], 20e6)
        return ad_envelope(waveform, info)

    # ---- reading ----------------------------------------------------------------------
    def _node(self, fn, ch):
        return self.dev.get_double(fn, ctypes.c_int(ch), ctypes.c_int(_NODE_CARRIER))

    def read_channel(self, ch: int, full: bool = True) -> dict:
        d = self.dev
        got = {"load_ohm": None, "mode": "continuous", "unread": []}
        try:
            sts = ctypes.c_ubyte()
            d.call("FDwfAnalogOutStatus", ctypes.c_int(ch), ctypes.byref(sts))
            # running (3) or armed / waiting (1, 7) = the output is ON  # VERIFY states
            got["output"] = sts.value in (1, 3, 7)
            self._on[ch] = got["output"]
            fn = ctypes.c_ubyte()
            d.call("FDwfAnalogOutNodeFunctionGet", ctypes.c_int(ch),
                   ctypes.c_int(_NODE_CARRIER), ctypes.byref(fn))
            wf = _FUNC_NAMES.get(fn.value, "arb")
            got["waveform"] = wf
            got["frequency_Hz"] = self._node("FDwfAnalogOutNodeFrequencyGet", ch)
            got["amplitude_Vpp"] = 2.0 * self._node("FDwfAnalogOutNodeAmplitudeGet", ch)
            got["offset_V"] = self._node("FDwfAnalogOutNodeOffsetGet", ch)
            got["phase_deg"] = self._node("FDwfAnalogOutNodePhaseGet", ch)
            sym = self._node("FDwfAnalogOutNodeSymmetryGet", ch)
            extra = self._shape_extra.get(ch, {"duty_pct": 50.0, "symmetry_pct": 50.0})
            # ONE dwf parameter: duty for a pulse / square, symmetry for a ramp
            got["duty_pct"] = sym if wf in ("pulse", "square") else extra["duty_pct"]
            got["symmetry_pct"] = sym if wf == "ramp" else extra["symmetry_pct"]
        except DwfError as exc:
            for k in ("output", "waveform", "frequency_Hz", "amplitude_Vpp", "offset_V",
                      "phase_deg", "duty_pct", "symmetry_pct"):
                got.setdefault(k, None)
                got["unread"].append(k)
            got["error"] = str(exc)
        return got

    # ---- writing ----------------------------------------------------------------------
    def _set(self, fn, ch, value) -> None:
        self.dev.call(fn, ctypes.c_int(ch), ctypes.c_int(_NODE_CARRIER), ctypes.c_double(value))
        self._apply(ch)

    def _running(self, ch: int) -> bool:
        sts = ctypes.c_ubyte()
        self.dev.call("FDwfAnalogOutStatus", ctypes.c_int(ch), ctypes.byref(sts))
        return sts.value in (1, 3, 7)

    def _apply(self, ch: int) -> None:
        """A parameter changed on a RUNNING output must be applied, or the
        output STOPS (lab AD2 2026-10-08, WaveForms 3.24.4: a new frequency or
        amplitude while W1 ran left it off, "output: asked True, instrument
        False"). Configure 3 = apply to the running channel (newer runtimes);
        where that is refused, 1 = (re)start it."""
        if not self._on.get(ch):
            return
        try:
            self.dev.call("FDwfAnalogOutConfigure", ctypes.c_int(ch), ctypes.c_int(3))
        except DwfError:
            self.dev.call("FDwfAnalogOutConfigure", ctypes.c_int(ch), ctypes.c_int(1))

    def _sync_start(self) -> None:
        """Both outputs restarted TOGETHER (W2 slaved to W1), so the phase
        between them is the phase set (lab AD2: W2 phase 90 set on its own
        while both ran gave -56 deg -- the two had started at different
        moments). Only when both run: a single output needs no partner."""
        if self._n < 2 or not (self._on.get(0) and self._on.get(1)):
            return
        d = self.dev
        d.call("FDwfAnalogOutMasterSet", ctypes.c_int(1), ctypes.c_int(0))      # VERIFY
        d.call("FDwfAnalogOutConfigure", ctypes.c_int(1), ctypes.c_int(1))      # W2 armed
        d.call("FDwfAnalogOutConfigure", ctypes.c_int(0), ctypes.c_int(1))      # W1 starts both

    def set_output(self, ch: int, on: bool) -> None:
        d = self.dev
        if on:
            d.call("FDwfAnalogOutNodeEnableSet", ctypes.c_int(ch), ctypes.c_int(_NODE_CARRIER),
                   ctypes.c_int(1))
        d.call("FDwfAnalogOutConfigure", ctypes.c_int(ch), ctypes.c_int(1 if on else 0))
        self._on[ch] = bool(on)
        if on:
            self._sync_start()

    def set_waveform(self, ch: int, waveform: str) -> None:
        if waveform not in _FUNC:
            raise ValueError(f"cannot select {waveform!r}")
        self.dev.call("FDwfAnalogOutNodeFunctionSet", ctypes.c_int(ch),
                      ctypes.c_int(_NODE_CARRIER), ctypes.c_ubyte(_FUNC[waveform]))
        extra = self._shape_extra.setdefault(ch, {"duty_pct": 50.0, "symmetry_pct": 50.0})
        if waveform in ("pulse", "square"):
            self._set("FDwfAnalogOutNodeSymmetrySet", ch, extra["duty_pct"])
        elif waveform == "ramp":
            self._set("FDwfAnalogOutNodeSymmetrySet", ch, extra["symmetry_pct"])
        else:
            self._apply(ch)

    def set_frequency(self, ch: int, hz: float) -> None:
        self._set("FDwfAnalogOutNodeFrequencySet", ch, float(hz))

    def set_amplitude(self, ch: int, vpp: float) -> None:
        self._set("FDwfAnalogOutNodeAmplitudeSet", ch, float(vpp) / 2.0)       # VERIFY peak

    def set_offset(self, ch: int, volts: float) -> None:
        self._set("FDwfAnalogOutNodeOffsetSet", ch, float(volts))

    def set_phase(self, ch: int, deg: float) -> None:
        self._set("FDwfAnalogOutNodePhaseSet", ch, float(deg) % 360.0)
        self._sync_start()          # a phase between two outputs needs a common start

    def set_duty(self, ch: int, pct: float) -> None:
        self._shape_extra.setdefault(ch, {"duty_pct": 50.0, "symmetry_pct": 50.0})["duty_pct"] = pct
        self._set("FDwfAnalogOutNodeSymmetrySet", ch, float(pct))

    def set_symmetry(self, ch: int, pct: float) -> None:
        self._shape_extra.setdefault(ch, {"duty_pct": 50.0, "symmetry_pct": 50.0})["symmetry_pct"] = pct
        self._set("FDwfAnalogOutNodeSymmetrySet", ch, float(pct))

    def set_load(self, ch: int, load_ohm) -> None:
        raise ValueError("the Analog Discovery's outputs have no load setting")

    def align_phase(self) -> None:
        """W2 slaved to W1, both restarted together: their phases then count
        from the same instant (lab AD2: follow at +90 + align -> 89.99 deg)."""
        for ch in range(self._n):
            self._on[ch] = self._running(ch)
        self._sync_start()

    def drain_errors(self) -> list[str]:
        return []

    def idn(self) -> str:
        return self.dev.idn()
