"""Real camera backend -- IDS uEye+ cameras via the IDS peak SDK.

The lab camera is a U3-386xCP-M (mono, 1936x1096, S/N 0000000000, FW 3.50.24150;
the original notes said U3-38J0XCP). IDS peak is its GenICam-based SDK.

VERIFIED on it 2026-09-13 (IDS peak 2.17.1, ids-peak 1.16, ids-peak-ipl 1.17.2):
open, idn, get/set_feature, 237 features, grab -> (1096, 1936) uint8 at 20 fps.
KNOWN: the camera's own power-on UserSet (Default) = 15 ms exposure, which
saturates this microscope completely (every pixel 255); ~1 ms gives mean ~76.
Since 2026-09-27 open() ADOPTS the camera's current settings and writes none of
them (it used to load the Default UserSet and force Mono8): a restarted service
keeps the exposure the camera was left at. After a camera POWER CYCLE the camera
boots its own default set again -- lower ExposureTime in the GUI, or store a good
set in the camera (UserSetDefault) once with IDS peak Cockpit.
This is the ONLY camera file that imports the IDS libraries, and it imports them
LAZILY inside :meth:`open` so the package still imports and the simulator still
runs on a PC with no IDS peak installed.  In ``pyproject.toml`` the IDS deps stay
COMMENTED OUT until the lab PC.

It exposes the camera's GenICam node map through the generic feature interface
(features()/get_feature/set_feature), so the GUI's "Camera parameters" panel drives
exposure, gain, gamma, frame rate, pixel format, ROI, etc. with no camera-specific
code.

Grab returns a 2-D **Mono8** uint8 array (colour models are converted to mono via
IDS peak IPL), which is what the vision engine wants. When the camera runs in a
deeper mono format (Mono10/12, packed or not), the SAME buffer is also converted
to 16-bit and kept for ``last_deep()`` -- the spot-size metrics use it
(2026-09-28). The PixelFormat itself is only ever read.

To finish at the microscope (a short hardware pass):
  1. Install "IDS peak" (includes the driver + Python wheels), then
     `pip install ids-peak ids-peak-ipl` (uncomment them in pyproject.toml).
  2. Plug in the U3-38J0XCP; confirm it appears in IDS peak Cockpit first.
  3. If you have several cameras, set ``device`` to the serial/display name to
     pick the right one (default = first device found).
  4. Tune exposure/gain from the GUI; open() never overwrites them.

References: ids_peak Library / DeviceManager / RemoteDevice().NodeMaps() /
DataStreams(); acquisition = AllocAndAnnounceBuffer -> QueueBuffer ->
AcquisitionStart -> WaitForFinishedBuffer.
"""

from __future__ import annotations

import re
import threading

import numpy as np

from . import claim as hwclaim

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
        # What the camera REPORTS after open() (read, never written -- see open).
        self.pixel_format = pixel_format
        self._lib_open = False
        self._dev = None
        self._nodemap = None
        self._stream = None
        self._ipl = None                # ids_peak_ipl module
        self._peak = None               # ids_peak module
        self._ext = None                # ids_peak_ipl_extension module
        self._lock = threading.Lock()   # serialise node-map access vs. grab
        self._hwlock = None             # our claim on this camera's serial (hwlock)
        self._deep = None               # (uint16 frame, bits) of the last grab
        self._deep_warned = False
        # Why there is no deep frame ("" = there is one, or nothing grabbed
        # yet). The brain logs it ONCE per start / per change (deep_note).
        self._deep_note = ""

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
        # Any failure below must leave nothing behind: not the IDS library, not
        # a half-open stream and above all not our claim on the camera -- a
        # failed start must not make the camera look "in use" to the next try.
        try:
            dm = ids_peak.DeviceManager.Instance()
            dm.Update()
            devices = dm.Devices()
            if not devices:
                raise RuntimeError("no IDS camera found (check USB3 + IDS peak Cockpit)")
            descr = self._pick_device(devices)
            # CLAIM before OpenDevice, i.e. before the first byte goes to the
            # camera. Enumerating devices (above) is the host listing the USB
            # bus; it does not talk to the camera's control channel. The claim
            # is the serial of the device actually FOUND, so "first found" and
            # "picked by display name" lock the same camera as "picked by serial".
            self._hwlock = hwclaim.claim(hwclaim.camera_address(descr.SerialNumber()))
            self._dev = descr.OpenDevice(ids_peak.DeviceAccessType_Control)
            self._nodemap = self._dev.RemoteDevice().NodeMaps()[0]

            # ADOPT, DO NOT RESET (Lukas, 2026-09-27: "all modules should read the
            # instrument state on startup, not to change anything"). This used to
            # load the factory Default UserSet and force PixelFormat = Mono8, which
            # threw away whatever exposure / gain / ROI the camera was running with
            # (the Default set is 15 ms = a saturated image on the microscope). Now
            # the camera keeps its settings; we only READ them. The pixel format is
            # read, not set: grab() converts any format to Mono8 in SOFTWARE (IDS
            # peak IPL), so the vision engine still gets 8-bit grayscale.
            self.pixel_format = self._read_pixel_format()

            # Opening the data stream + AcquisitionStart is the ONE write left at
            # start: without it the camera delivers no frames at all. It changes no
            # camera parameter (exposure, gain, ROI, format all stay as found).
            self._start_stream()
        except BaseException:
            # close() only talks to the camera if the stream was opened (then the
            # camera is ours); after a HardwareBusy nothing was opened, so it only
            # closes our own IDS library handle. It also releases the claim.
            self.close()
            raise

    def _read_pixel_format(self) -> str:
        """The camera's current PixelFormat, read only ("" if unreadable)."""
        try:
            return str(self._nodemap.FindNode("PixelFormat").CurrentEntry().SymbolicValue())
        except Exception:
            return ""

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

    def close(self) -> None:
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
        # Release LAST: the camera is only free for another service once we
        # have stopped using it.
        if self._hwlock is not None:
            self._hwlock.release()
            self._hwlock = None

    def idn(self) -> str:
        try:
            return f"IDS {self._nodemap.FindNode('DeviceModelName').Value()}"
        except Exception:
            return "IDS uEye+ camera"

    def grab(self) -> np.ndarray:
        buffer = self._stream.WaitForFinishedBuffer(2000)
        # The buffer goes back to the camera WHATEVER happens below. It used to
        # be re-queued only after a successful conversion: every frame that
        # failed to convert (a corrupt/incomplete buffer, a pixel format the
        # conversion refuses) kept one of the few announced buffers, and after
        # that many failures the camera had nowhere to put a frame -- no image
        # until the service was restarted (deep cleaning 2026-09-28).
        self._deep = None
        try:
            img = self._ext.BufferToImage(buffer)
            # Convert to Mono8 so the vision engine gets a 2-D grayscale array.
            # Since open() no longer forces Mono8, this conversion also has to cope
            # with whatever format the camera was left in (Mono10/12, packed).
            # VERIFY: ConvertTo(Mono8) from Mono10p/Mono12p on the U3-386xCP-M.
            mono = img.ConvertTo(self._ipl.PixelFormatName_Mono8)
            arr = mono.get_numpy_2D().copy()
            # ... and, from the SAME buffer, the full-depth frame for the spot
            # size (2026-09-28): at 8 bit the far-defocused spot's wings are
            # below one grey level and sigma^2 read up to 50 % low on the rig.
            # Only when the camera already runs deeper than 8 bit: we READ its
            # PixelFormat (open), never set it (adopt rule).
            self._deep = self._convert_deep(img)
        finally:
            # VERIFY: re-queueing a buffer whose conversion failed (IDS peak docs:
            # a buffer is reusable once handed back, whatever its content).
            self._stream.QueueBuffer(buffer)
        return arr

    def _convert_deep(self, img):
        """(uint16 frame in 0 .. 2^bits - 1, bits) of this image, or None.

        None for a Mono8 camera, and when the conversion fails -- the 8-bit
        frame is then used for everything, as before; a deep frame is a bonus,
        never a reason to lose a frame.
        """
        bits = bit_depth(self.pixel_format)
        if bits <= 8:
            # Nothing to convert -- but SAY so (rig 2026-09-29: the metrics ran
            # on 8 bit and nothing told why). The PixelFormat is the operator's
            # to change: this module only reads it (adopt rule).
            self._deep_note = (
                f"the camera's PixelFormat is {self.pixel_format or 'unknown'}, so the "
                f"spot size metrics run on 8-bit frames; Mono10 / Mono12 would give "
                f"10 / 12-bit metrics (this module never changes PixelFormat -- set "
                f"it in IDS peak Cockpit, then restart the camera service)")
            return None
        # Unpacked target of the same depth: IDS peak IPL names Mono10 /
        # Mono12 / Mono16 (2 bytes per pixel).
        # VERIFY on the U3-386xCP-M: (1) ConvertTo(Mono12) from Mono12g24IDS /
        # Mono12p / Mono12 and ConvertTo(Mono10) from Mono10g40IDS / Mono10p;
        # (2) that get_numpy_2D() of the result is (h, w) uint16; (3) whether
        # the value is LSB-aligned (0..4095) or MSB-aligned (x16) -- both are
        # handled below, by looking at the data.
        name = {10: "PixelFormatName_Mono10", 12: "PixelFormatName_Mono12"}.get(
            bits, "PixelFormatName_Mono16")
        try:
            target = getattr(self._ipl, name)
            deep = np.asarray(img.ConvertTo(target).get_numpy_2D())
            if deep.ndim != 2:
                return None
            deep = deep.astype(np.uint16, copy=True)
            top = (1 << bits) - 1
            low = (1 << (16 - bits)) - 1
            if bits < 16 and (int(deep.max()) > top
                              or (deep.any() and not (deep & low).any())):
                # MSB-aligned (the value in the TOP bits of the 16, so either
                # above the depth's maximum or with the low bits always 0 --
                # noise makes real LSB data odd somewhere): shift down
                deep >>= (16 - bits)
            self._deep_note = ""
            return deep, bits
        except Exception as exc:
            self._deep_note = (f"no {bits}-bit frame from {self.pixel_format} "
                               f"({type(exc).__name__}: {exc}); spot sizes use 8 bit")
            if not self._deep_warned:
                self._deep_warned = True
                print(f"camera: {self._deep_note}")
            return None

    def last_deep(self):
        """(full-depth frame, bits) of the last grab() -- the SAME buffer -- or None."""
        return self._deep

    def deep_note(self) -> str:
        """Why the last grab() left no deep frame ("" when it did). ASCII."""
        return self._deep_note

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
                if name == "PixelFormat":
                    # the user changed it (live panel): the deep frame follows
                    self.pixel_format = str(value)
                    self._deep_warned = False
            elif t == peak.NodeType_Boolean:
                node.SetValue(bool(value))
            elif t == peak.NodeType_Integer:
                node.SetValue(int(round(float(value))))
            elif t == peak.NodeType_Float:
                node.SetValue(float(value))
            else:
                node.SetValue(str(value))


# -- small helpers (module level; no hardware) ----------------------------- #
def bit_depth(pixel_format: str) -> int:
    """Bits per pixel of a MONO pixel-format name; 8 for anything else.

    "Mono12g24IDS" (IDS's packed 12 bit, what the lab camera was left in),
    "Mono12p", "Mono12" -> 12; "Mono10..." -> 10; "Mono16" -> 16; "Mono8",
    "" (unread) or a colour format -> 8 (no deep frame).
    """
    m = re.match(r"Mono(\d+)", str(pixel_format or ""))
    if not m:
        return 8
    bits = int(m.group(1))
    return bits if bits in (10, 12, 14, 16) else 8


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
