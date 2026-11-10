"""The Thorlabs (Zelux CS165MU) backend against a FAKE thorlabs_tsi_sdk: it
adopts the camera's settings (writes only the acquisition set-up), claims the
camera's serial, delivers Mono8 + the 10-bit deep frame from ONE buffer, and
offers exposure / gain / binning through the generic feature model. Offline."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from camera import hwlock
from camera.backends.thorlabs import ThorlabsCamera
from camera.hwlock import HardwareBusy


class FakeFrame:
    def __init__(self, buf):
        self.image_buffer = buf


class FakeTLCamera:
    """The attributes of thorlabs_tsi_sdk's TLCamera the backend uses."""

    def __init__(self, serial):
        self.serial_number = serial
        self.model = "CS165MU"
        self.bit_depth = 10
        self.sensor_width_pixels, self.sensor_height_pixels = 1440, 1080
        self.image_width_pixels, self.image_height_pixels = 64, 48
        # what the camera was LEFT at by a previous session (must be adopted)
        self.exposure_time_us = 2345
        self.exposure_time_range_us = (40, 26843000)
        self.gain = 7
        self.gain_range = (0, 480)
        self.black_level = 12
        self.black_level_range = (0, 100)
        self.binx = self.biny = 1
        self.binx_range = self.biny_range = (1, 16)
        self.writes: list[tuple] = []
        self.armed = False
        self.frames_per_trigger_zero_for_unlimited = 1
        self.image_poll_timeout_ms = 0
        self.operation_mode = "HARDWARE"
        self.disposed = False
        self.pending = 0
        self._ready = True

    #: the real SDK passes these through c_int / c_longlong: a float raises
    #: TypeError there (checked in thorlabs_tsi_sdk 1.2.0's tl_camera.py)
    INT_ATTRS = ("exposure_time_us", "gain", "black_level", "binx", "biny",
                 "frames_per_trigger_zero_for_unlimited", "image_poll_timeout_ms")

    @property
    def is_armed(self):
        return self.armed

    def __setattr__(self, name, value):
        if getattr(self, "_ready", False):
            if name in self.INT_ATTRS and not isinstance(value, int):
                raise TypeError(f"{name}: int expected instead of {type(value).__name__}")
            self.writes.append((name, value))
            if name in ("binx", "biny") and self.armed:
                raise RuntimeError("cannot change binning while armed")
        object.__setattr__(self, name, value)

    def arm(self, n):
        object.__setattr__(self, "armed", True)
        self.writes.append(("arm", n))

    def disarm(self):
        object.__setattr__(self, "armed", False)
        self.writes.append(("disarm",))

    def issue_software_trigger(self):
        self.writes.append(("trigger",))

    def get_pending_frame_or_null(self):
        if not self.armed:
            return None
        h, w = self.image_height_pixels, self.image_width_pixels
        buf = (np.arange(h * w, dtype=np.uint16).reshape(h, w) % 1024)
        buf[0, 0] = 1023
        return FakeFrame(buf)

    def dispose(self):
        object.__setattr__(self, "disposed", True)


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    state = {"serials": ["12345"], "cams": []}

    class TLCameraSDK:
        def discover_available_cameras(self):
            return list(state["serials"])

        def open_camera(self, serial):
            cam = FakeTLCamera(serial)
            state["cams"].append(cam)
            return cam

        def dispose(self):
            pass

    mod = types.SimpleNamespace(TLCameraSDK=TLCameraSDK,
                                OPERATION_MODE=types.SimpleNamespace(SOFTWARE_TRIGGERED="SW"))
    pkg = types.ModuleType("thorlabs_tsi_sdk")
    pkg.tl_camera = mod
    monkeypatch.setitem(sys.modules, "thorlabs_tsi_sdk", pkg)
    monkeypatch.setitem(sys.modules, "thorlabs_tsi_sdk.tl_camera", mod)
    return state


def test_open_adopts_settings_and_only_sets_up_acquisition(sdk):
    cam = ThorlabsCamera()
    cam.open()
    try:
        fake = sdk["cams"][0]
        written = {w[0] for w in fake.writes}
        # acquisition set-up only -- never exposure / gain / black level / binning
        assert written <= {"frames_per_trigger_zero_for_unlimited", "image_poll_timeout_ms",
                           "operation_mode", "arm", "trigger"}
        assert cam.get_feature("ExposureTime") == 2345 and cam.get_feature("Gain") == 7
        assert cam.pixel_format == "Mono10"
        assert "CS165MU" in cam.idn() and "12345" in cam.idn()
    finally:
        cam.close()
    assert sdk["cams"][0].disposed


def test_grab_gives_mono8_and_the_10bit_frame_from_one_buffer(sdk):
    cam = ThorlabsCamera()
    cam.open()
    try:
        img = cam.grab()
        deep, bits = cam.last_deep()
        assert img.dtype == np.uint8 and img.shape == (48, 64)
        assert deep.dtype == np.uint16 and bits == 10
        assert int(deep[0, 0]) == 1023 and int(img[0, 0]) == 255
        assert np.array_equal(img, (deep >> 2).astype(np.uint8))
        assert cam.deep_note() == ""
    finally:
        cam.close()


def test_features_and_setting_them(sdk):
    cam = ThorlabsCamera()
    cam.open()
    try:
        feats = {f["name"]: f for f in cam.features()}
        assert feats["ExposureTime"]["unit"] == "us"
        assert (feats["Gain"]["min"], feats["Gain"]["max"]) == (0, 480)
        assert feats["DeviceModelName"]["writable"] is False
        cam.set_feature("ExposureTime", 500.0)
        assert cam.get_feature("ExposureTime") == 500.0
        # binning needs the camera disarmed: done around the write, re-armed after
        cam.set_feature("BinningHorizontal", 2)
        fake = sdk["cams"][0]
        assert fake.binx == 2 and fake.armed
        with pytest.raises(KeyError):
            cam.set_feature("DeviceModelName", "x")
    finally:
        cam.close()


def test_a_second_service_on_the_same_camera_is_refused(sdk):
    a = ThorlabsCamera()
    a.open()
    try:
        with pytest.raises(HardwareBusy, match="camera"):
            ThorlabsCamera(device="12345").open()
        assert len(sdk["cams"]) == 1           # the refused one never opened it
    finally:
        a.close()
    assert hwlock.held() == []


def test_no_camera_or_wrong_serial_says_so(sdk):
    sdk["serials"] = []
    with pytest.raises(RuntimeError, match="no Thorlabs camera"):
        ThorlabsCamera().open()
    sdk["serials"] = ["111"]
    with pytest.raises(RuntimeError, match="found: 111"):
        ThorlabsCamera(device="999").open()
    assert hwlock.held() == []


# --------------------------------------------------------------------------- #
# 2026-11-09, after reading the real SDK (thorlabs_tsi_sdk 1.2.0)
# --------------------------------------------------------------------------- #
def test_exposure_is_written_as_whole_microseconds(sdk):
    """The SDK's setter is c_longlong: a float raised TypeError (the brain's
    auto exposure and AF exposure send floats)."""
    cam = ThorlabsCamera()
    cam.open()
    try:
        cam.set_feature("ExposureTime", 63.59)
        fake = sdk["cams"][0]
        assert ("exposure_time_us", 64) in fake.writes
        cam.set_feature("Gain", 12.0)
        assert fake.gain == 12 and isinstance(fake.gain, int)
    finally:
        cam.close()


def test_an_unsupported_gain_or_black_level_is_not_offered(sdk):
    cam = ThorlabsCamera()
    cam.open()
    try:
        fake = sdk["cams"][0]
        object.__setattr__(fake, "gain_range", (0, 0))       # SDK: max 0 = not supported
        object.__setattr__(fake, "black_level_range", (0, 0))
        names = {f["name"] for f in cam.features()}
        assert "Gain" not in names and "BlackLevel" not in names
        assert "ExposureTime" in names
    finally:
        cam.close()


def test_a_camera_left_armed_is_disarmed_before_the_set_up(sdk, monkeypatch):
    class Armed(FakeTLCamera):
        def __init__(self, serial):
            super().__init__(serial)
            object.__setattr__(self, "armed", True)

    sdk_mod = sys.modules["thorlabs_tsi_sdk.tl_camera"]

    def open_armed(self, serial):
        c = Armed(serial)
        sdk["cams"].append(c)
        return c
    monkeypatch.setattr(sdk_mod.TLCameraSDK, "open_camera", open_armed)
    cam = ThorlabsCamera()
    cam.open()
    try:
        w = [x[0] for x in sdk["cams"][0].writes]
        assert w.index("disarm") < w.index("frames_per_trigger_zero_for_unlimited")
    finally:
        cam.close()


def test_one_poll_blocks_only_a_short_slice(sdk):
    """The lock is held during a poll: the SDK's poll timeout is set short
    (POLL_SLICE_MS), grab() keeps its own deadline."""
    from camera.backends.thorlabs import POLL_SLICE_MS
    cam = ThorlabsCamera(poll_timeout_ms=2000)
    cam.open()
    try:
        assert sdk["cams"][0].image_poll_timeout_ms == POLL_SLICE_MS <= 100
    finally:
        cam.close()


def test_the_package_dlls_win_thorcams_folder_is_only_a_fallback(sdk, monkeypatch, tmp_path):
    import camera.backends.thorlabs as T
    added = []
    monkeypatch.setattr(T, "_add_dll_dir", lambda d, keep: added.append(d) or True)
    monkeypatch.setattr(T, "DEFAULT_DLL_DIRS", (str(tmp_path),))
    cam = ThorlabsCamera()
    cam.open()
    cam.close()
    assert added == []                       # nothing added when the SDK loads
    # the package's own DLLs fail to load -> ThorImageCAM's folder, once
    mod = sys.modules["thorlabs_tsi_sdk.tl_camera"]
    good = mod.TLCameraSDK
    tries = []

    def flaky():
        tries.append(1)
        if len(tries) == 1:
            raise RuntimeError("Unable to load library: thorlabs_tsi_camera_sdk.dll")
        return good()
    monkeypatch.setattr(mod, "TLCameraSDK", flaky)
    cam = ThorlabsCamera()
    cam.open()
    cam.close()
    assert added == [str(tmp_path)] and len(tries) == 2
    # a configured DLL folder is the user's choice: no silent fallback
    tries.clear()
    added.clear()
    monkeypatch.setattr(T, "preload_sdk_dll", lambda d, keep: d)
    with pytest.raises(RuntimeError, match="Unable to load"):
        ThorlabsCamera(dll_dir=str(tmp_path)).open()
    assert len(tries) == 1


def _fake_sdk_folder(home, version):
    d = home / f"thorlabs_tsi_sdk-{version}"
    (d / "thorlabs_tsi_sdk").mkdir(parents=True)
    (d / "thorlabs_tsi_sdk" / "__init__.py").write_text("", encoding="utf-8")
    (d / "thorlabs_tsi_sdk" / "tl_camera.py").write_text(
        f"VERSION = {version!r}\n", encoding="utf-8")
    (d / "lib" / "windows-x86_64").mkdir(parents=True)
    return d


def _forget_sdk(monkeypatch):
    """No thorlabs_tsi_sdk in this test's world: not imported, and no folder
    holding one on sys.path. On a rig PC (Blue Box, 2026-11-10) the REAL SDK
    from camera-control/vendor was already imported and its folder on the
    path, so a test that builds a fake SDK folder found the real one."""
    for k in [k for k in sys.modules if k.startswith("thorlabs_tsi_sdk")]:
        monkeypatch.delitem(sys.modules, k)
    import os
    monkeypatch.setattr(sys, "path", [x for x in sys.path if "site-packages" in x
                                      or not os.path.isdir(os.path.join(x, "thorlabs_tsi_sdk"))])


def test_the_sdk_is_found_in_the_vendor_folder_newest_version_first(monkeypatch, tmp_path):
    """Not installed (uv sync removes it) -> loaded from
    %LOCALAPPDATA%/AaltoFlow/vendor/thorlabs_tsi_sdk-*, the newest version."""
    import camera.backends.thorlabs as T
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(T, "default_sdk_home", lambda: str(tmp_path / "module-vendor"))
    home = tmp_path / "AaltoFlow" / "vendor"
    _fake_sdk_folder(home, "1.9.0")
    newest = _fake_sdk_folder(home, "1.10.0")
    assert T.find_sdk_dir() == str(newest)
    _forget_sdk(monkeypatch)
    monkeypatch.setattr(sys, "path", list(sys.path))
    keep = []
    monkeypatch.setattr(T, "_add_dll_dir", lambda d, k: keep.append(d) or True)
    try:
        tl = T.import_tl_camera()
        assert tl.VERSION == "1.10.0"
        assert keep == [str(newest / "lib" / "windows-x86_64")]
    finally:
        for k in [k for k in sys.modules if k.startswith("thorlabs_tsi_sdk")]:
            del sys.modules[k]


def test_no_sdk_anywhere_says_how_to_get_it(monkeypatch, tmp_path):
    import builtins

    import camera.backends.thorlabs as T
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(T, "default_sdk_home", lambda: str(tmp_path / "module-vendor"))
    _forget_sdk(monkeypatch)
    real_import = builtins.__import__

    def no_sdk(name, *a, **kw):
        if name.startswith("thorlabs_tsi_sdk"):
            raise ImportError(name)
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_sdk)
    with pytest.raises(RuntimeError, match="setup_thorlabs_sdk.py"):
        T.import_tl_camera()


def _setup_script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "setup_thorlabs_sdk.py"
    spec = importlib.util.spec_from_file_location("setup_thorlabs_sdk", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _package_zip_bytes(extra=()):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("thorlabs_tsi_sdk-1.2.0/thorlabs_tsi_sdk/__init__.py", "")
        z.writestr("thorlabs_tsi_sdk-1.2.0/thorlabs_tsi_sdk/tl_camera.py", "X = 1\n")
        z.writestr("thorlabs_tsi_sdk-1.2.0/lib/windows-x86_64/thorlabs_tsi_camera_sdk.dll", b"MZ")
        for name, data in extra:
            z.writestr(name, data)
    return buf.getvalue()


def test_setup_script_unpacks_from_the_sdk_download_or_the_inner_zip(tmp_path, capsys):
    import zipfile
    S = _setup_script()
    outer = tmp_path / "scientific_camera_interfaces-2.3.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("Scientific Camera Interfaces/Python_README.txt", "readme")
        z.writestr("Scientific Camera Interfaces/SDK/Python Toolkit/"
                   "thorlabs_tsi_camera_python_sdk_package.zip", _package_zip_bytes())
    dest = tmp_path / "vendor"
    assert S.main([str(outer), "--dest", str(dest)]) == 0
    root = dest / "thorlabs_tsi_sdk-1.2.0"
    assert (root / "thorlabs_tsi_sdk" / "tl_camera.py").is_file()
    assert (root / "lib" / "windows-x86_64" / "thorlabs_tsi_camera_sdk.dll").is_file()
    out = capsys.readouterr().out
    assert out.isascii() and "present" in out          # gotcha #14: printed text is ASCII
    inner = tmp_path / "thorlabs_tsi_camera_python_sdk_package.zip"
    inner.write_bytes(_package_zip_bytes())
    assert S.main([str(inner), "--dest", str(tmp_path / "v2")]) == 0
    # and the driver finds what the script made
    from camera.backends.thorlabs import find_sdk_dir
    assert find_sdk_dir(str(root)) == str(root)


def test_setup_script_refuses_an_entry_outside_its_folder(tmp_path):
    S = _setup_script()
    evil = tmp_path / "evil.zip"
    evil.write_bytes(_package_zip_bytes(extra=[("thorlabs_tsi_sdk-1.2.0/../../x.txt", "x")]))
    with pytest.raises(SystemExit, match="outside"):
        S.main([str(evil), "--dest", str(tmp_path / "v")])
    assert not (tmp_path / "x.txt").exists()


def test_the_real_sdk_loads_when_it_is_unpacked_here(monkeypatch):
    """Runs only where the REAL SDK is unpacked (setup_thorlabs_sdk.py, or
    AALTOFLOW_THORLABS_SDK = its folder): loads Thorlabs' DLL, lists cameras
    (an empty list without one), disposes. Opens no camera. It uses the SAME
    DLLs as the service: camera.ini's thorlabs_sdk_dir / thorlabs_dll_dir
    (AALTOFLOW_THORLABS_SDK / AALTOFLOW_THORLABS_DLL_DIR override them) -- on
    the Blue Box the bundled 2.3 DLLs crash and camera.ini names ThorCam's."""
    import os

    from camera import probe as P
    from camera.backends.thorlabs import find_sdk_dir, import_tl_camera
    ini_sdk, ini_dll = P._thorlabs_dirs()
    d = find_sdk_dir(os.environ.get("AALTOFLOW_THORLABS_SDK", "") or ini_sdk)
    if not d:
        pytest.skip("the real Thorlabs SDK is not unpacked on this PC")
    dll_dir = os.environ.get("AALTOFLOW_THORLABS_DLL_DIR", "") or ini_dll
    _forget_sdk(monkeypatch)                 # never a fake left by another test
    monkeypatch.setattr(sys, "path", list(sys.path))
    tl = import_tl_camera(d, dll_dir)
    sdk = tl.TLCameraSDK()
    try:
        assert isinstance(sdk.discover_available_cameras(), list)
    finally:
        sdk.dispose()



# --------------------------------------------------------------------------- #
# 2026-11-10, Blue Box rig: the bundled SDK 2.3 DLLs crash in
# tl_camera_open_sdk on that PC (access violation); ThorCam's 2.1 DLLs work.
# An explicit dll_dir must WIN, and the SDK copy must not live in %LOCALAPPDATA%
# (redirected for programs started from the Claude app).
# --------------------------------------------------------------------------- #
def test_the_module_vendor_folder_is_looked_in_first(monkeypatch, tmp_path):
    import camera.backends.thorlabs as T
    mod_home = tmp_path / "module-vendor"
    monkeypatch.setattr(T, "default_sdk_home", lambda: str(mod_home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _fake_sdk_folder(tmp_path / "AaltoFlow" / "vendor", "1.9.9")    # the old place, newer
    in_module = _fake_sdk_folder(mod_home, "1.2.0")
    assert T.find_sdk_dir() == str(in_module)
    assert T.sdk_homes()[0] == str(mod_home)


def test_the_default_home_is_camera_control_vendor():
    from pathlib import Path

    import camera.backends.thorlabs as T
    root = Path(__file__).resolve().parents[1]
    assert Path(T.default_sdk_home()) == root / "vendor"
    assert Path(_setup_script().default_dest()) == root / "vendor"


def test_an_explicit_dll_dir_is_preloaded_and_the_bundled_dlls_stay_off(monkeypatch, tmp_path):
    """The bundled lib/windows-x86_64 used to be put on the path AFTER dll_dir,
    so the 2.3 DLL always won. Now dll_dir is preloaded by its full path and
    the bundled folder is not added at all."""
    import camera.backends.thorlabs as T
    sdk_dir = _fake_sdk_folder(tmp_path / "v", "1.2.0")
    dll_dir = tmp_path / "ThorCam"
    dll_dir.mkdir()
    order = []
    monkeypatch.setattr(T, "preload_sdk_dll", lambda d, keep: order.append(("preload", d)))
    monkeypatch.setattr(T, "_add_dll_dir", lambda d, keep: order.append(("add", d)) or True)
    _forget_sdk(monkeypatch)
    monkeypatch.setattr(sys, "path", list(sys.path))
    try:
        T.import_tl_camera(str(sdk_dir), str(dll_dir))
    finally:
        for k in [k for k in sys.modules if k.startswith("thorlabs_tsi_sdk")]:
            del sys.modules[k]
    assert order == [("preload", str(dll_dir))]          # nothing bundled added
    # without dll_dir: the bundled folder, no preload
    order.clear()
    _forget_sdk(monkeypatch)
    monkeypatch.setattr(sys, "path", [x for x in sys.path if x != str(sdk_dir)])
    try:
        T.import_tl_camera(str(sdk_dir), "")
    finally:
        for k in [k for k in sys.modules if k.startswith("thorlabs_tsi_sdk")]:
            del sys.modules[k]
    assert order == [("add", str(sdk_dir / "lib" / "windows-x86_64"))]


def test_preload_needs_the_sdk_dll_in_that_folder(tmp_path):
    import camera.backends.thorlabs as T
    with pytest.raises(RuntimeError, match="thorlabs_tsi_camera_sdk.dll"):
        T.preload_sdk_dll(str(tmp_path), [])


def test_an_sdk_that_crashes_on_start_says_to_set_the_dll_folder(sdk, monkeypatch):
    mod = sys.modules["thorlabs_tsi_sdk.tl_camera"]

    def crash():
        raise OSError("exception: access violation reading 0x0000000000000000")
    monkeypatch.setattr(mod, "TLCameraSDK", crash)
    with pytest.raises(RuntimeError, match="thorlabs_dll_dir.*ThorCam"):
        ThorlabsCamera().open()
    assert hwlock.held() == []


def test_the_probe_uses_the_dll_folder_from_camera_ini(monkeypatch):
    """The probe must load the SAME DLLs as the service, or it crashes where
    the service works."""
    from camera import probe as P
    from camera.backends import thorlabs as T
    seen = {}
    monkeypatch.setattr(P, "_thorlabs_dirs", lambda: ("S", "D"))
    monkeypatch.setattr(T, "sdk_available", lambda sdk_dir="": seen.setdefault("avail", sdk_dir) or True)

    class SDK:
        def discover_available_cameras(self):
            return ["00000042"]

        def dispose(self):
            pass

    def imp(sdk_dir="", dll_dir="", keep=None):
        seen["import"] = (sdk_dir, dll_dir)
        return types.SimpleNamespace(TLCameraSDK=SDK)
    monkeypatch.setattr(T, "import_tl_camera", imp)
    rows, note = P._thorlabs()
    assert seen == {"avail": "S", "import": ("S", "D")}
    assert [r["address"] for r in rows] == ["00000042"] and note == ""
