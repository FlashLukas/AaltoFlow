"""Real camera backend -- Thorlabs scientific cameras (Zelux / Kiralux /
Quantalux) via the Thorlabs Scientific Camera SDK (thorlabs_tsi_sdk).

The vacuum setup's camera (2026-11-09) is a CS165MU: Zelux, 1.6 MP monochrome,
1440 x 1080, 10-bit, USB 3. It is NOT GenICam and NOT the old DCx/uc480 line --
ThorCam talks to it through the "TSI" SDK, whose Python wrapper ships INSIDE the
ThorCam install (not on PyPI):
    C:\\Program Files\\Thorlabs\\Scientific Imaging\\Scientific Camera Support\\
        Scientific Camera Interfaces.zip  ->  SDK\\Python Toolkit\\
        thorlabs_tsi_camera_python_sdk_package.zip      (pip install that zip)
and needs the native DLLs (thorlabs_tsi_camera_sdk.dll ...) on the DLL search
path: the "SDK\\Native Toolkit\\dlls\\Native_64_lib" folder of the same zip, or
ThorCam's own install folder. Point ``dll_dir`` (camera.thorlabs_dll_dir) at it;
empty = try ThorCam's default folder.

This is the ONLY camera file that imports the Thorlabs SDK, LAZILY inside
:meth:`open`, so the package imports and the simulator runs without it.
CLOSE ThorCam before starting the service: it holds the camera.

ADOPT, DO NOT RESET (Lukas's rule, 2026-09-27): open() reads exposure, gain,
black level, ROI and binning and writes none of them. The only writes are the
acquisition set-up the camera needs to deliver frames at all (continuous
software-triggered streaming: frames_per_trigger = 0, arm, one trigger) -- the
same role AcquisitionStart has on the IDS camera.

FRAMES: the sensor delivers 10 bits in a uint16 buffer. grab() returns the
8-bit frame the vision engine wants (the top 8 bits) and keeps the full 10-bit
frame for ``last_deep()``: the spot-size metrics (D4sigma, Z step calibration)
use it -- on the 8-bit IDS camera the far-defocused wings read up to 50 % low.

FEATURES: exposure, gain, black level, binning and a few read-only facts are
offered through the generic feature model (features / get_feature /
set_feature), named like GenICam ("ExposureTime" in us ...) so the brain's auto
exposure and autofocus exposure work as on the IDS camera.

Not yet run on the camera: every SDK call is marked # VERIFY.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from . import claim as hwclaim

#: Where ThorCam installs its DLLs by default (the TSI SDK DLLs are there too).
DEFAULT_DLL_DIRS = (
    r"C:\Program Files\Thorlabs\Scientific Imaging\ThorCam",
    r"C:\Program Files\Thorlabs\ThorImageCAM\Bin",
)


class ThorlabsCamera:
    def __init__(self, device: str = "", dll_dir: str = "", poll_timeout_ms: int = 2000):
        self.device = str(device or "").strip()   # serial; "" = the first one found
        self.dll_dir = str(dll_dir or "").strip()
        self.poll_timeout_ms = int(poll_timeout_ms)
        self.pixel_format = "Mono10"              # what the sensor delivers (read at open)
        self._sdk = None
        self._cam = None
        self._mod = None                          # the tl_camera module
        self._dll_handle = None
        self._hwlock = None
        self._lock = threading.Lock()             # SDK calls vs. grab
        self._bits = 10
        self._serial = ""
        self._model = ""
        self._deep = None
        self._deep_note = ""

    # ------------------------------------------------------------------ #
    def open(self) -> None:
        self._add_dll_dir()
        try:
            from thorlabs_tsi_sdk import tl_camera          # VERIFY: package name
        except ImportError as exc:  # pragma: no cover - only on a real PC
            raise RuntimeError(
                "thorlabs_tsi_sdk not installed: pip install the "
                "thorlabs_tsi_camera_python_sdk_package.zip from the ThorCam install "
                "into this environment (see backends/thorlabs.py)") from exc
        self._mod = tl_camera
        try:
            self._sdk = tl_camera.TLCameraSDK()             # VERIFY
            found = list(self._sdk.discover_available_cameras())   # VERIFY: serials
            if not found:
                raise RuntimeError("no Thorlabs camera found (USB 3 plugged in? ThorCam "
                                   "closed? -- it holds the camera)")
            serial = self.device if self.device in found else (
                found[0] if not self.device else None)
            if serial is None:
                raise RuntimeError(f"Thorlabs camera {self.device!r} not found "
                                   f"(found: {', '.join(found)})")
            # CLAIM before the camera is opened (one camera, one service): the
            # serial of the camera actually found, so "" and the serial lock the
            # same camera.
            self._hwlock = hwclaim.claim(hwclaim.camera_address(serial))
            self._serial = str(serial)
            self._cam = self._sdk.open_camera(serial)       # VERIFY
            cam = self._cam
            self._bits = int(getattr(cam, "bit_depth", 10) or 10)        # VERIFY
            self.pixel_format = f"Mono{self._bits}"
            self._model = str(getattr(cam, "model", "") or "")           # VERIFY
            # The acquisition set-up (not a camera PARAMETER): stream frames
            # continuously, started by one software trigger.
            cam.frames_per_trigger_zero_for_unlimited = 0                # VERIFY
            cam.image_poll_timeout_ms = max(100, self.poll_timeout_ms)   # VERIFY
            self._arm()
        except BaseException:
            self.close()
            raise

    def _add_dll_dir(self) -> None:
        """Put the TSI native DLLs on the search path (Windows, Python >= 3.8:
        PATH is no longer searched for a DLL that a DLL loads)."""
        dirs = [self.dll_dir] if self.dll_dir else list(DEFAULT_DLL_DIRS)
        for d in dirs:
            if d and os.path.isdir(d):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                add = getattr(os, "add_dll_directory", None)
                if add is not None:
                    try:
                        self._dll_handle = add(d)
                    except OSError:
                        pass
                return

    def _arm(self) -> None:
        cam = self._cam
        try:
            cam.operation_mode = self._mod.OPERATION_MODE.SOFTWARE_TRIGGERED   # VERIFY
        except Exception:
            pass                                    # older SDKs: the default already
        cam.arm(2)                                  # VERIFY: 2 frame buffers
        cam.issue_software_trigger()                # VERIFY: starts the stream

    def _disarm(self) -> None:
        try:
            self._cam.disarm()                      # VERIFY
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._cam is not None:
                self._disarm()
                self._cam.dispose()                 # VERIFY
        except Exception:
            pass
        try:
            if self._sdk is not None:
                self._sdk.dispose()                 # VERIFY
        except Exception:
            pass
        self._cam = self._sdk = None
        if self._dll_handle is not None:
            try:
                self._dll_handle.close()
            except Exception:
                pass
            self._dll_handle = None
        # release LAST: the camera is free for another service only now
        if self._hwlock is not None:
            self._hwlock.release()
            self._hwlock = None

    def idn(self) -> str:
        return f"Thorlabs {self._model or 'camera'} S/N {self._serial}".strip()

    # ------------------------------------------------------------------ #
    def grab(self) -> np.ndarray:
        """The next frame as Mono8 (2-D uint8); the full-depth frame is kept
        for last_deep(). Waits up to poll_timeout_ms for a frame."""
        deadline = time.monotonic() + self.poll_timeout_ms / 1000.0
        self._deep = None
        while True:
            with self._lock:
                frame = self._cam.get_pending_frame_or_null()            # VERIFY
            if frame is not None:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"no frame from the Thorlabs camera in "
                                   f"{self.poll_timeout_ms} ms")
            time.sleep(0.002)
        raw = np.asarray(frame.image_buffer)                             # VERIFY: (h, w) uint16
        if raw.ndim == 1:
            h = int(getattr(self._cam, "image_height_pixels", 0) or 0)   # VERIFY
            w = int(getattr(self._cam, "image_width_pixels", 0) or 0)
            raw = raw.reshape(h, w)
        deep = raw.astype(np.uint16, copy=True)
        bits = self._bits
        self._deep = (deep, bits) if bits > 8 else None
        self._deep_note = "" if bits > 8 else "the camera delivers 8-bit frames"
        shift = max(0, bits - 8)
        return (deep >> shift).astype(np.uint8)

    def last_deep(self):
        """(full-depth frame, bits) of the last grab() -- the SAME frame -- or None."""
        return self._deep

    def deep_note(self) -> str:
        return self._deep_note

    # ------------------------------------------------------------------ #
    # the feature model (named like GenICam, so the brain's exposure logic works)
    # ------------------------------------------------------------------ #
    #: name -> (SDK attribute, its range attribute, type, unit, needs disarm)
    _FEATURES = {
        "ExposureTime": ("exposure_time_us", "exposure_time_range_us", "float", "us", False),
        "Gain": ("gain", "gain_range", "int", "", False),
        "BlackLevel": ("black_level", "black_level_range", "int", "", False),
        "BinningHorizontal": ("binx", "binx_range", "int", "", True),
        "BinningVertical": ("biny", "biny_range", "int", "", True),
    }
    #: read-only facts shown in the panel
    _FACTS = {
        "DeviceModelName": "model", "DeviceSerialNumber": "serial_number",
        "BitDepth": "bit_depth", "SensorWidth": "sensor_width_pixels",
        "SensorHeight": "sensor_height_pixels", "Width": "image_width_pixels",
        "Height": "image_height_pixels",
    }

    def features(self) -> list:
        out = []
        with self._lock:
            cam = self._cam
            for name, (attr, rng, typ, unit, _disarm) in self._FEATURES.items():
                try:
                    val = getattr(cam, attr)                              # VERIFY
                except Exception:
                    continue
                lo = hi = None
                try:
                    r = getattr(cam, rng)                                 # VERIFY: (min, max)
                    lo, hi = r[0], r[1]
                except Exception:
                    pass
                out.append({"name": name, "display": _pretty(name), "type": typ,
                            "value": val, "min": lo, "max": hi,
                            "inc": 1 if typ == "int" else None, "unit": unit,
                            "options": None, "writable": True,
                            "category": "Thorlabs"})
            for name, attr in self._FACTS.items():
                try:
                    val = getattr(cam, attr)
                except Exception:
                    continue
                typ = "int" if isinstance(val, int) and not isinstance(val, bool) else "string"
                out.append({"name": name, "display": _pretty(name), "type": typ,
                            "value": val if typ == "int" else str(val),
                            "min": None, "max": None, "inc": None, "unit": "",
                            "options": None, "writable": False, "category": "Thorlabs"})
        return out

    def get_feature(self, name: str):
        with self._lock:
            if name in self._FEATURES:
                return getattr(self._cam, self._FEATURES[name][0])
            if name in self._FACTS:
                return getattr(self._cam, self._FACTS[name])
        raise KeyError(f"unknown camera feature {name!r}")

    def set_feature(self, name: str, value) -> None:
        if name not in self._FEATURES:
            raise KeyError(f"camera feature {name!r} is not writable")
        attr, _rng, typ, _unit, needs_disarm = self._FEATURES[name]
        v = float(value) if typ == "float" else int(round(float(value)))
        with self._lock:
            if needs_disarm:
                # the SDK refuses ROI/binning changes while armed (VERIFY)
                self._disarm()
                try:
                    setattr(self._cam, attr, v)
                finally:
                    self._arm()
            else:
                setattr(self._cam, attr, v)                               # VERIFY


def _pretty(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper():
            out.append(" ")
        out.append(ch)
    return "".join(out)
