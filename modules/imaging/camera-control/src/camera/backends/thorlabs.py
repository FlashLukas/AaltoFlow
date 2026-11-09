r"""Real camera backend -- Thorlabs scientific cameras (Zelux / Kiralux /
Quantalux) via the Thorlabs Scientific Camera SDK (thorlabs_tsi_sdk).

The vacuum setup's camera (2026-11-09) is a CS165MU: Zelux, 1.6 MP monochrome,
1440 x 1080, 10-bit, USB 3. It is NOT GenICam and NOT the old DCx/uc480 line --
it speaks Thorlabs' "TSI" SDK. Checked against the real SDK on 2026-11-09:
"Windows SDK and Doc. for Scientific Cameras" 2.3 from thorlabs.com (ThorCam
page > Programming Interfaces), whose SDK\Python Toolkit\
thorlabs_tsi_camera_python_sdk_package.zip holds thorlabs_tsi_sdk 1.2.0.

WHERE THE SDK COMES FROM. It is not on PyPI, so it cannot be a dependency in
pyproject.toml -- and a copy pip-installed into the venv is REMOVED by the next
``uv sync`` (gotcha #29: sync removes what the lock does not list). So the
driver also loads it from a FOLDER outside the venv: ``sdk_dir``
(camera.thorlabs_sdk_dir), or by default the newest
%LOCALAPPDATA%\AaltoFlow\vendor\thorlabs_tsi_sdk-* that
``scripts/setup_thorlabs_sdk.py <the SDK zip>`` unpacks. An installed package
wins when there is one.

DLLs. Since 1.2.0 the Python package carries its own native DLLs
(lib/windows-x86_64) and the driver puts that folder on the search path. It
adds another folder only when told (``dll_dir`` = camera.thorlabs_dll_dir), or
as a LAST resort when the package's own DLLs failed to load: two different
versions of thorlabs_tsi_camera_sdk.dll in one process is how mysterious
crashes start. The camera's USB DRIVER comes with ThorImageCAM / ThorCam:
install that first.

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

Not yet run on the camera: every SDK call is marked # VERIFY (names and
types checked against the SDK source; the behaviour on the camera is not).
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from . import claim as hwclaim

#: Where ThorImageCAM / ThorCam install their DLLs -- tried only if the Python
#: package's own DLLs could not be loaded (see the module docstring).
DEFAULT_DLL_DIRS = (
    r"C:\Program Files\Thorlabs\ThorImageCAM\Bin",
    r"C:\Program Files\Thorlabs\Scientific Imaging\ThorCam",
)

#: How long ONE poll call may block in the SDK. Short on purpose: the
#: backend's lock is held during the call, and a settings change from the GUI
#: waits for it. grab() keeps polling until its own deadline.
POLL_SLICE_MS = 50


def default_sdk_home() -> str:
    """Where scripts/setup_thorlabs_sdk.py unpacks the Python SDK."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AaltoFlow", "vendor")


def find_sdk_dir(sdk_dir: str = "") -> str:
    """The folder that HOLDS the thorlabs_tsi_sdk package (it has a
    ``thorlabs_tsi_sdk`` subfolder): ``sdk_dir`` if given, else the newest
    thorlabs_tsi_sdk-* in default_sdk_home(); "" if there is none."""
    if sdk_dir:
        return sdk_dir if os.path.isdir(os.path.join(sdk_dir, "thorlabs_tsi_sdk")) else ""
    home = default_sdk_home()
    try:
        names = [n for n in os.listdir(home) if n.startswith("thorlabs_tsi_sdk-")]
    except OSError:
        return ""

    def version(name):
        out = []
        for part in name.split("-", 1)[1].split("."):
            out.append(int(part) if part.isdigit() else -1)
        return out

    for n in sorted(names, key=version, reverse=True):     # newest first
        d = os.path.join(home, n)
        if os.path.isdir(os.path.join(d, "thorlabs_tsi_sdk")):
            return d
    return ""


def sdk_available(sdk_dir: str = "") -> bool:
    """Is the Python SDK installed or unpacked? (no DLL is loaded)"""
    import importlib.util
    try:
        if importlib.util.find_spec("thorlabs_tsi_sdk") is not None:
            return True
    except (ImportError, ValueError):
        pass
    return bool(find_sdk_dir(sdk_dir))


def _add_dll_dir(d: str, keep: list) -> bool:
    """Put one DLL folder on the search path (Windows, Python >= 3.8: PATH is
    no longer searched for a DLL that a DLL loads, hence add_dll_directory)."""
    if not d or not os.path.isdir(d):
        return False
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    add = getattr(os, "add_dll_directory", None)
    if add is not None:
        try:
            keep.append(add(d))
        except OSError:
            pass
    return True


def import_tl_camera(sdk_dir: str = "", dll_dir: str = "", keep: list | None = None):
    """thorlabs_tsi_sdk.tl_camera: the installed package, else the one in
    find_sdk_dir(sdk_dir). An unpacked package's own DLL folder
    (lib/windows-x86_64) goes on the search path; ``dll_dir`` too when given.
    ``keep`` collects the add_dll_directory handles (close them on close)."""
    import sys
    keep = keep if keep is not None else []
    if dll_dir:
        _add_dll_dir(dll_dir, keep)
    try:
        from thorlabs_tsi_sdk import tl_camera
        return tl_camera
    except ImportError:
        pass
    found = find_sdk_dir(sdk_dir)
    if not found:
        where = sdk_dir or default_sdk_home()
        raise RuntimeError(
            "thorlabs_tsi_sdk not found (looked in " + where + "): run "
            "'python scripts/setup_thorlabs_sdk.py <scientific_camera_interfaces-*.zip>' "
            "once (thorlabs.com, ThorCam page > Programming Interfaces), or set "
            "camera.thorlabs_sdk_dir")
    bits = "x86_64" if sys.maxsize > 2 ** 32 else "x86"
    _add_dll_dir(os.path.join(found, "lib", f"windows-{bits}"), keep)
    if found not in sys.path:
        sys.path.insert(0, found)
    from thorlabs_tsi_sdk import tl_camera
    return tl_camera


class ThorlabsCamera:
    def __init__(self, device: str = "", dll_dir: str = "", poll_timeout_ms: int = 2000,
                 sdk_dir: str = ""):
        self.device = str(device or "").strip()   # serial; "" = the first one found
        self.dll_dir = str(dll_dir or "").strip()
        self.sdk_dir = str(sdk_dir or "").strip()
        self.poll_timeout_ms = int(poll_timeout_ms)
        self.pixel_format = "Mono10"              # what the sensor delivers (read at open)
        self._sdk = None
        self._cam = None
        self._mod = None                          # the tl_camera module
        self._dll_handles: list = []
        self._hwlock = None
        self._lock = threading.Lock()             # SDK calls vs. grab
        self._bits = 10
        self._serial = ""
        self._model = ""
        self._deep = None
        self._deep_note = ""

    # ------------------------------------------------------------------ #
    def open(self) -> None:
        tl_camera = import_tl_camera(self.sdk_dir, self.dll_dir, self._dll_handles)
        self._mod = tl_camera
        try:
            self._sdk = self._new_sdk(tl_camera)
            # serial strings; discovering opens nothing
            found = list(self._sdk.discover_available_cameras())
            if not found:
                raise RuntimeError("no Thorlabs camera found (USB 3 plugged in? ThorImageCAM / "
                                   "ThorCam closed? -- they hold the camera)")
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
            # A camera left ARMED (by a crashed process) refuses the set-up below.
            try:
                if getattr(cam, "is_armed", False):                       # VERIFY
                    cam.disarm()
            except Exception:
                pass
            # The acquisition set-up (not a camera PARAMETER): stream frames
            # continuously, started by one software trigger -- the SDK's own
            # recipe for continuous video (TLCamera.arm docstring).
            cam.frames_per_trigger_zero_for_unlimited = 0                # VERIFY
            # host side: how long ONE poll call may block (see POLL_SLICE_MS)
            cam.image_poll_timeout_ms = POLL_SLICE_MS                    # VERIFY
            self._arm()
        except BaseException:
            self.close()
            raise

    def _new_sdk(self, tl_camera):
        """TLCameraSDK(); if the package's own DLLs do not load and no DLL
        folder was configured, try ThorImageCAM's / ThorCam's folder ONCE."""
        try:
            return tl_camera.TLCameraSDK()
        except Exception as exc:
            if self.dll_dir or "load" not in str(exc).lower():
                raise
            if not any([_add_dll_dir(d, self._dll_handles) for d in DEFAULT_DLL_DIRS]):
                raise
            return tl_camera.TLCameraSDK()                   # VERIFY: the fallback

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
        for h in self._dll_handles:
            try:
                h.close()
            except Exception:
                pass
        self._dll_handles = []
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
                # blocks at most POLL_SLICE_MS; the buffer is valid only until
                # the next poll (SDK docs), so it is copied while still ours
                frame = self._cam.get_pending_frame_or_null()            # VERIFY
                raw = None if frame is None else np.array(frame.image_buffer, copy=True)
            if frame is not None:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"no frame from the Thorlabs camera in "
                                   f"{self.poll_timeout_ms} ms")
            time.sleep(0.001)       # the SDK already waited; this only spares a CPU
        # raw: (h, w) uint16; the low `bits` bits are the pixel (SDK docs)
        if raw.ndim == 1:
            h = int(getattr(self._cam, "image_height_pixels", 0) or 0)   # VERIFY
            w = int(getattr(self._cam, "image_width_pixels", 0) or 0)
            raw = raw.reshape(h, w)
        deep = raw.astype(np.uint16, copy=False)
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
                # the SDK's rule: a range whose maximum is 0 = not supported by
                # this model (gain / black level on some cameras)
                if hi == 0 and name in ("Gain", "BlackLevel"):
                    continue
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
        # EVERY TSI setter takes an integer (exposure is a c_longlong of whole
        # microseconds): a float raises TypeError inside the SDK. Exposure is
        # still offered as "float" so the GUI and the brain's exposure logic
        # treat it like the IDS one; it is rounded here.
        v = int(round(float(value)))
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
