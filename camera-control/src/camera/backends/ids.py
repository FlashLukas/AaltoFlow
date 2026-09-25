"""Real camera backend -- IDS uEye+ cameras via the IDS peak SDK.

The lab camera is a U3-386xCP-M (mono, 1936x1096, S/N 0000000000, FW 3.50.24150;
the original notes said U3-38J0XCP). IDS peak is its GenICam-based SDK.

VERIFIED on it 2026-09-13 (IDS peak 2.17.1, ids-peak 1.16, ids-peak-ipl 1.17.2):
open, idn, get/set_feature, 237 features, grab -> (1096, 1936) uint8 at 20 fps.
KNOWN: open() loads the Default UserSet = 15 ms exposure, which saturates this
microscope completely (every pixel 255); ~1 ms gives mean ~76. Lower
ExposureTime in the GUI after starting, until this is made configurable.
This is the ONLY camera file that imports the IDS libraries, and it imports them
LAZILY inside :meth:`open` so the package still imports and the simulator still
runs on a PC with no IDS peak installed.  In ``pyproject.toml`` the IDS deps stay
COMMENTED OUT until the lab PC.

It exposes the camera's GenICam node map through the generic feature interface
(features()/get_feature/set_feature), so the GUI's "Camera parameters" panel drives
exposure, gain, gamma, frame rate, pixel format, ROI, etc. with no camera-specific
code.

Grab returns a 2-D **Mono8** uint8 array (colour models are converted to mono via
IDS peak IPL), which is what the vision engine wants.

To finish at the microscope (a short hardware pass):
  1. Install "IDS peak" (includes the driver + Python wheels), then
     `pip install ids-peak ids-peak-ipl` (uncomment them in pyproject.toml).
  2. Plug in the U3-38J0XCP; confirm it appears in IDS peak Cockpit first.
  3. If you have several cameras, set ``device`` to the serial/display name to
     pick the right one (default = first device found).
  4. Verify the default UserSet gives a usable image, then tune from the GUI.

References: ids_peak Library / DeviceManager / RemoteDevice().NodeMaps() /
DataStreams(); acquisition = AllocAndAnnounceBuffer -> QueueBuffer ->
AcquisitionStart -> WaitForFinishedBuffer.
"""

from __future__ import annotations

import threading

import numpy as np

# Which GenICam visibilities to surface in the GUI (skip Guru clutter).
_VISIBILITIES = {"Beginner", "Expert"}
# A friendly default order for the common controls (others appended after).
_PREFERRED = [
    "ExposureTime", "ExposureAuto", "AcquisitionFrameRate", "Gain", "GainAuto",
    "Gamma", "BlackLevel", "PixelFormat", "Width", "Height", "OffsetX", "OffsetY",
    "BinningHorizontal", "BinningVertical",
]


class IDSCamera:
    def __init__(self, device: str = "", pixel_format: str = "Mono8"):
        self.device = device            # serial / display name; "" = first found
        self.pixel_format = pixel_format
        self._lib_open = False
        self._dev = None
        self._nodemap = None
        self._stream = None
        self._ipl = None                # ids_peak_ipl module
        self._peak = None               # ids_peak module
        self._ext = None                # ids_peak_ipl_extension module
        self._lock = threading.Lock()   # serialise node-map access vs. grab

    # ------------------------------------------------------------------ #
    def open(self) -> None:
        try:
            from ids_peak import ids_peak, ids_peak_ipl_extension
            from ids_peak_ipl import ids_peak_ipl
        except ImportError as exc:  # pragma: no cover - only on a real PC
            raise RuntimeError(
                "ids_peak not installed. Install 'IDS peak' and "
                "`pip install ids-peak ids-peak-ipl` (see ids.py header)."
            ) from exc
        self._peak, self._ipl, self._ext = ids_peak, ids_peak_ipl, ids_peak_ipl_extension

        ids_peak.Library.Initialize()
        self._lib_open = True
        dm = ids_peak.DeviceManager.Instance()
        dm.Update()
        devices = dm.Devices()
        if not devices:
            raise RuntimeError("no IDS camera found (check USB3 + IDS peak Cockpit)")
        descr = self._pick_device(devices)
        self._dev = descr.OpenDevice(ids_peak.DeviceAccessType_Control)
        self._nodemap = self._dev.RemoteDevice().NodeMaps()[0]

        # Load the factory default UserSet for a known starting state.
        try:
            self._nodemap.FindNode("UserSetSelector").SetCurrentEntry("Default")
            self._nodemap.FindNode("UserSetLoad").Execute()
        except Exception:
            pass
        try:
            self._nodemap.FindNode("PixelFormat").SetCurrentEntry(self.pixel_format)
        except Exception:
            pass

        self._start_stream()

    def _pick_device(self, devices):
        if self.device:
            for d in devices:
                try:
                    if self.device in (d.SerialNumber(), d.DisplayName()):
                        return d
                except Exception:
                    continue
        return devices[0]

    def _start_stream(self):  # pragma: no cover - only on a real PC
        self._stream = self._dev.DataStreams()[0].OpenDataStream()
        payload = self._nodemap.FindNode("PayloadSize").Value()
        for _ in range(self._stream.NumBuffersAnnouncedMinRequired()):
            buf = self._stream.AllocAndAnnounceBuffer(payload)
            self._stream.QueueBuffer(buf)
        self._stream.StartAcquisition()
        self._nodemap.FindNode("AcquisitionStart").Execute()

    def close(self) -> None:  # pragma: no cover - only on a real PC
        try:
            if self._stream is not None:
                self._nodemap.FindNode("AcquisitionStop").Execute()
                self._stream.StopAcquisition()
                self._stream.Flush(self._peak.DataStreamFlushMode_DiscardAll)
                for buf in self._stream.AnnouncedBuffers():
                    self._stream.RevokeBuffer(buf)
        except Exception:
            pass
        self._stream = self._dev = self._nodemap = None
        if self._lib_open:
            try:
                self._peak.Library.Close()
            except Exception:
                pass
            self._lib_open = False

    def idn(self) -> str:
        try:
            return f"IDS {self._nodemap.FindNode('DeviceModelName').Value()}"
        except Exception:
            return "IDS uEye+ camera"

    def grab(self) -> np.ndarray:  # pragma: no cover - only on a real PC
        buffer = self._stream.WaitForFinishedBuffer(2000)
        img = self._ext.BufferToImage(buffer)
        # Convert to Mono8 so the vision engine gets a 2-D grayscale array.
        mono = img.ConvertTo(self._ipl.PixelFormatName_Mono8)
        arr = mono.get_numpy_2D().copy()
        self._stream.QueueBuffer(buffer)
        return arr

    # ------------------------------------------------------------------ #
    # feature model over the GenICam node map
    # ------------------------------------------------------------------ #
    def features(self) -> list:  # pragma: no cover - only on a real PC
        with self._lock:
            found = {}
            for node in self._nodemap.Nodes():
                try:
                    d = self._describe(node)
                except Exception:
                    d = None
                if d is not None:
                    found[d["name"]] = d
        # order: preferred first, then the rest alphabetically
        ordered = [found.pop(n) for n in _PREFERRED if n in found]
        ordered += [found[k] for k in sorted(found)]
        return ordered

    def _describe(self, node):  # pragma: no cover - only on a real PC
        peak = self._peak
        name = node.Name()
        # skip hidden/unavailable/invisible nodes
        if node.Visibility() not in _visset(peak):
            return None
        acc = node.AccessStatus()
        readable = acc in (peak.NodeAccessStatus_ReadOnly, peak.NodeAccessStatus_ReadWrite)
        writable = acc == peak.NodeAccessStatus_ReadWrite
        if not readable and node.Type() != peak.NodeType_Command:
            return None
        t = node.Type()
        base = {"name": name, "display": _pretty(name), "unit": "",
                "min": None, "max": None, "inc": None, "options": None,
                "writable": writable, "category": ""}
        if t == peak.NodeType_Float:
            base.update(type="float", value=node.Value(), min=node.Minimum(),
                        max=node.Maximum(), unit=_unit(node))
            try:
                if node.HasConstantIncrement():
                    base["inc"] = node.Increment()
            except Exception:
                pass
        elif t == peak.NodeType_Integer:
            base.update(type="int", value=node.Value(), min=node.Minimum(),
                        max=node.Maximum(), inc=node.Increment(), unit=_unit(node))
        elif t == peak.NodeType_Boolean:
            base.update(type="bool", value=bool(node.Value()))
        elif t == peak.NodeType_Enumeration:
            entries = [e.SymbolicValue() for e in node.Entries()
                       if e.AccessStatus() != peak.NodeAccessStatus_NotAvailable]
            base.update(type="enum", value=node.CurrentEntry().SymbolicValue(),
                        options=entries)
        elif t == peak.NodeType_Command:
            base.update(type="command")
        elif t == peak.NodeType_String:
            base.update(type="string", value=node.Value())
        else:
            return None
        return base

    def get_feature(self, name: str):  # pragma: no cover - only on a real PC
        with self._lock:
            node = self._nodemap.FindNode(name)
            peak = self._peak
            t = node.Type()
            if t == peak.NodeType_Enumeration:
                return node.CurrentEntry().SymbolicValue()
            if t == peak.NodeType_Command:
                return None
            return node.Value()

    def set_feature(self, name: str, value) -> None:  # pragma: no cover - real PC
        with self._lock:
            node = self._nodemap.FindNode(name)
            peak = self._peak
            t = node.Type()
            if t == peak.NodeType_Command:
                node.Execute()
                node.WaitUntilDone()
            elif t == peak.NodeType_Enumeration:
                node.SetCurrentEntry(str(value))
            elif t == peak.NodeType_Boolean:
                node.SetValue(bool(value))
            elif t == peak.NodeType_Integer:
                node.SetValue(int(round(float(value))))
            elif t == peak.NodeType_Float:
                node.SetValue(float(value))
            else:
                node.SetValue(str(value))


# -- small helpers (module level; no hardware) ----------------------------- #
def _visset(peak):  # pragma: no cover - only on a real PC
    out = set()
    for v in _VISIBILITIES:
        node_v = getattr(peak, f"NodeVisibility_{v}", None)
        if node_v is not None:
            out.add(node_v)
    return out


def _unit(node):  # pragma: no cover - only on a real PC
    try:
        return node.Unit()
    except Exception:
        return ""


def _pretty(name: str) -> str:
    """CamelCase GenICam name -> spaced label, e.g. ExposureTime -> Exposure Time."""
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper():
            out.append(" ")
        out.append(ch)
    return "".join(out)
