"""Real camera backend -- GenICam / GigE-USB3 Vision via Harvester.

This is the ONLY camera file that imports the hardware library, and it imports it
LAZILY inside :meth:`open` -- never at module top -- so the whole package still
imports and the simulator still runs on a PC with no machine-vision SDK.  In
``pyproject.toml`` the ``harvesters`` dependency stays COMMENTED OUT until the
lab PC.

To finish this at the microscope (a short hardware pass):
  1. `pip install harvesters` (uncomment it in pyproject.toml).
  2. Point ``cti_path`` at your vendor's GenTL producer .cti file (e.g. the one
     shipped with your camera's SDK, or NI/Matrix Vision's mvGenTLProducer.cti).
  3. Confirm the pixel format is Mono8 (or convert in :meth:`grab`).

Implements the :class:`camera.backends.base.CameraBackend` Protocol.
"""

from __future__ import annotations

import numpy as np

from . import claim as hwclaim


class GenICamCamera:
    def __init__(self, device: str = "0", cti_path: str = "",
                 video_mode: str = ""):
        self.device = device
        self.cti_path = cti_path
        self.video_mode = video_mode
        self._h = None      # Harvester
        self._ia = None     # ImageAcquirer
        self._hwlock = None # our claim on this camera's serial (hwlock)
        self.pixel_format = ""  # READ at open (never written): sets the bit depth
        self._deep = None       # (uint16 frame, bits) of the last grab

    def open(self) -> None:
        # Lazy import: keeps the package importable without the SDK installed.
        try:
            from harvesters.core import Harvester
        except ImportError as exc:  # pragma: no cover - only on a real PC
            raise RuntimeError(
                "harvesters not installed. `pip install harvesters` and set a "
                "valid GenTL .cti producer path (see genicam.py header)."
            ) from exc

        self._h = Harvester()
        # Any failure below must release everything, the claim included, so a
        # failed start never leaves the camera looking "in use".
        try:
            if self.cti_path:
                self._h.add_file(self.cti_path)
            self._h.update()
            if not self._h.device_info_list:
                raise RuntimeError("no GenICam devices found (check the .cti path)")
            # Select by index if the id is numeric, else by serial/id string.
            try:
                selector = int(self.device)
            except ValueError:
                selector = {"id_": self.device}
            # CLAIM before create(): create() opens the device's control
            # channel, the first contact with the camera itself. The claim is
            # the SERIAL of the device the selector resolves to -- the same
            # address the IDS backend claims for the same camera.
            info = self._resolve(selector)
            self._hwlock = hwclaim.claim(hwclaim.camera_address(_serial_of(info)))
            self._ia = self._h.create(selector)
            try:                              # read only (adopt rule)
                self.pixel_format = str(self._ia.remote_device.node_map.PixelFormat.value)
            except Exception:
                self.pixel_format = ""
            self._ia.start()
        except BaseException:
            self.close()
            raise

    def _resolve(self, selector):
        """The device_info entry `selector` will open (index or {"id_": ...})."""
        infos = self._h.device_info_list
        if isinstance(selector, int):
            if not 0 <= selector < len(infos):
                raise RuntimeError(f"no GenICam device #{selector} ({len(infos)} found)")
            return infos[selector]
        want = selector["id_"]
        for info in infos:
            if getattr(info, "id_", None) == want:
                return info
        raise RuntimeError(f"no GenICam device with id {want!r}")

    def close(self) -> None:
        try:
            if self._ia is not None:
                try:
                    self._ia.stop()
                finally:
                    self._ia.destroy()
                    self._ia = None
            if self._h is not None:
                self._h.reset()
                self._h = None
        finally:
            # Release LAST, and even if the SDK teardown raised.
            if self._hwlock is not None:
                self._hwlock.release()
                self._hwlock = None

    def idn(self) -> str:
        return f"GenICam camera {self.device!r}"

    def grab(self) -> np.ndarray:
        """One frame as 8-bit; a deeper frame is also kept for last_deep().

        A 10/12/16-bit mono camera hands Harvester 16-bit data. Until
        2026-09-28 that went to the brain as it was, and ``vision.to_gray``
        STRETCHED every frame to its own min..max -- the brightness of the
        image then depended on what was in it. Now it is scaled by the bit
        depth (the top 8 bits), the same for every frame, and the 16-bit
        data is kept for the spot-size metrics, like the IDS backend does.
        VERIFY on a real GenICam camera: that the data are unpacked (a packed
        Mono12p arrives as bytes, not uint16 -- then no deep frame) and
        LSB-aligned; the pixel format is READ at open, never set.
        """
        self._deep = None
        with self._ia.fetch() as buffer:
            comp = buffer.payload.components[0]
            img = np.array(comp.data.reshape(comp.height, comp.width), copy=True)
        if img.dtype == np.uint16:
            bits = _mono_bits(getattr(self, "pixel_format", ""))
            if bits <= 8:
                bits = 16 if int(img.max()) > 4095 else 12   # unread format: a guess
            self._deep = (img, bits)
            return (img >> (bits - 8)).clip(0, 255).astype(np.uint8)
        return img

    def last_deep(self):
        """(full-depth frame, bits) of the last grab(), or None (8-bit camera)."""
        return getattr(self, "_deep", None)

    # -- feature model (best-effort over the Harvester node map) ----------- #
    def features(self) -> list:  # pragma: no cover - only on a real PC
        # The native IDS backend (ids.py) is the primary path with full feature
        # enumeration; for the generic Harvester route we expose a small, common
        # set so the GUI panel still works.
        out = []
        for name in ("ExposureTime", "Gain", "Gamma", "AcquisitionFrameRate",
                     "BlackLevel"):
            try:
                node = getattr(self._ia.remote_device.node_map, name)
                out.append({"name": name, "display": name, "type": "float",
                            "value": float(node.value),
                            "min": float(getattr(node, "min", None) or 0),
                            "max": float(getattr(node, "max", None) or 0),
                            "inc": None, "unit": "", "options": None,
                            "writable": True, "category": ""})
            except Exception:
                continue
        return out

    def get_feature(self, name: str):  # pragma: no cover - only on a real PC
        return getattr(self._ia.remote_device.node_map, name).value

    def set_feature(self, name: str, value) -> None:  # pragma: no cover - real PC
        getattr(self._ia.remote_device.node_map, name).value = value


def _mono_bits(pixel_format: str) -> int:
    """Bits of a mono pixel-format name ("Mono12" -> 12); 8 if unknown."""
    from .ids import bit_depth           # one parser for both backends (no SDK import)
    return bit_depth(pixel_format)


def _serial_of(info) -> str:
    """Serial number from a Harvester device_info, falling back to its id.

    VERIFY: harvesters exposes ``serial_number`` on device_info (1.x); a GenTL
    producer that leaves it empty still gives a unique ``id_`` per device.
    """
    for attr in ("serial_number", "id_"):
        v = getattr(info, attr, None)
        if v:
            return str(v)
    return ""
