"""One physical instrument, one service: the camera module's hardware claims.

Offline: the vendor SDKs (IDS peak, harvesters, pylablib) are replaced by tiny
fakes injected into sys.modules, so the REAL backends' open()/close() run
exactly as on the lab PC, minus the hardware. The lock folder is a temp dir
(AALTOFLOW_LOCK_DIR), so these tests never touch a running service's locks.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from camera import hwlock
from camera.backends.claim import camera_address
from camera.backends.genicam import GenICamCamera
from camera.backends.ids import IDSCamera
from camera.backends.kcube import KCubeZFocus

SERIAL = "4108812345"


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


def test_copy_is_identical_to_the_master():
    # tools/check_modules.py checks this too; failing here says it earlier.
    root = Path(__file__).resolve().parents[4]
    master = root / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("suite-common not next to this module (installed alone)")
    mine = Path(hwlock.__file__)
    assert mine.read_bytes() == master.read_bytes()


# --------------------------------------------------------------------------- #
# fake IDS peak
# --------------------------------------------------------------------------- #
class _Node:
    def __init__(self, calls, name):
        self._calls, self._name = calls, name

    def Value(self):
        return 1024

    def Execute(self):
        self._calls.append(("execute", self._name))

    def CurrentEntry(self):
        return types.SimpleNamespace(SymbolicValue=lambda: "Mono8")


class _NodeMap:
    def __init__(self, calls):
        self._calls = calls

    def FindNode(self, name):
        return _Node(self._calls, name)


class _Stream:
    def __init__(self, calls):
        self._calls = calls

    def NumBuffersAnnouncedMinRequired(self):
        return 2

    def AllocAndAnnounceBuffer(self, n):
        return object()

    def QueueBuffer(self, b):
        pass

    def StartAcquisition(self):
        self._calls.append(("start_acq",))

    def StopAcquisition(self):
        self._calls.append(("stop_acq",))

    def Flush(self, mode):
        pass

    def AnnouncedBuffers(self):
        return []

    def RevokeBuffer(self, b):
        pass


class _Device:
    def __init__(self, calls):
        self._calls = calls
        nm = _NodeMap(calls)
        self.RemoteDevice = lambda: types.SimpleNamespace(NodeMaps=lambda: [nm])
        stream = _Stream(calls)
        self.DataStreams = lambda: [types.SimpleNamespace(OpenDataStream=lambda: stream)]


class _Descr:
    def __init__(self, calls, serial, name, fail=False):
        self._calls, self._serial, self._name, self._fail = calls, serial, name, fail

    def SerialNumber(self):
        return self._serial

    def DisplayName(self):
        return self._name

    def OpenDevice(self, access):
        self._calls.append(("open_device", self._serial))
        if self._fail:
            raise RuntimeError("fake: device open failed")
        return _Device(self._calls)


@pytest.fixture
def fake_ids(monkeypatch):
    """Install a fake ids_peak / ids_peak_ipl. Returns (calls, state)."""
    calls: list = []
    state = {"fail": False, "serial": SERIAL}

    class DeviceManager:
        @staticmethod
        def Instance():
            return types.SimpleNamespace(
                Update=lambda: None,
                Devices=lambda: [_Descr(calls, state["serial"], "U3-386xCP-M",
                                        state["fail"])])

    peak = types.SimpleNamespace(
        Library=types.SimpleNamespace(Initialize=lambda: calls.append(("lib_init",)),
                                      Close=lambda: calls.append(("lib_close",))),
        DeviceManager=DeviceManager,
        DeviceAccessType_Control=1,
        DataStreamFlushMode_DiscardAll=0,
    )
    pkg = types.ModuleType("ids_peak")
    pkg.ids_peak = peak
    pkg.ids_peak_ipl_extension = types.SimpleNamespace()
    ipl_pkg = types.ModuleType("ids_peak_ipl")
    ipl_pkg.ids_peak_ipl = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "ids_peak", pkg)
    monkeypatch.setitem(sys.modules, "ids_peak_ipl", ipl_pkg)
    return calls, state


def _held_addresses():
    return [h["normalized"] for h in hwlock.held()]


def test_ids_open_claims_the_serial_and_close_releases(fake_ids):
    cam = IDSCamera(device="")            # "" = first found: claim what was found
    cam.open()
    assert _held_addresses() == [camera_address(SERIAL).upper()]
    cam.close()
    assert hwlock.held() == []
    cam2 = IDSCamera(device="")           # (c) released -> a new open succeeds
    cam2.open()
    cam2.close()


def test_second_ids_on_same_camera_is_refused_and_sends_nothing(fake_ids):
    calls, _ = fake_ids
    a = IDSCamera(device=SERIAL)
    a.open()
    try:
        calls.clear()
        # (b) written differently: picked by display name, not by serial --
        # it resolves to the same camera, so it is the same address.
        b = IDSCamera(device="U3-386xCP-M")
        with pytest.raises(hwlock.HardwareBusy, match="camera"):
            b.open()
        # the refused backend never opened the camera ...
        assert ("open_device", SERIAL) not in calls
        # ... and its cleanup sent it nothing either (no AcquisitionStop etc.)
        assert not [c for c in calls if c[0] in ("execute", "stop_acq")]
        b.close()                          # a second close is harmless
    finally:
        a.close()
    assert hwlock.held() == []


def test_failed_ids_open_releases_the_claim(fake_ids):
    _, state = fake_ids
    state["fail"] = True
    cam = IDSCamera()
    with pytest.raises(RuntimeError, match="device open failed"):
        cam.open()
    assert hwlock.held() == []
    state["fail"] = False
    cam.open()                             # (d) nothing left behind
    cam.close()


# --------------------------------------------------------------------------- #
# fake harvesters (generic GenICam path) -- same camera, same address
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_harvesters(monkeypatch):
    state = {"fail": False}

    class _IA:
        def start(self):
            if state["fail"]:
                raise RuntimeError("fake: start failed")

        def stop(self):
            pass

        def destroy(self):
            pass

    class Harvester:
        def __init__(self):
            self.device_info_list = []

        def add_file(self, p):
            pass

        def update(self):
            self.device_info_list = [types.SimpleNamespace(
                serial_number=f" {SERIAL} ", id_="IDS_U3-386xCP-M_1")]

        def create(self, selector):
            return _IA()

        def reset(self):
            pass

    core = types.ModuleType("harvesters.core")
    core.Harvester = Harvester
    monkeypatch.setitem(sys.modules, "harvesters", types.ModuleType("harvesters"))
    monkeypatch.setitem(sys.modules, "harvesters.core", core)
    return state


def test_ids_and_genicam_on_the_same_camera_conflict(fake_ids, fake_harvesters):
    # The rule is about the BOX, not the driver: the serial is reported with
    # padding by the fake Harvester and selected by index, still one camera.
    ids = IDSCamera()
    ids.open()
    try:
        g = GenICamCamera(device="0")
        with pytest.raises(hwlock.HardwareBusy, match="camera"):
            g.open()
    finally:
        ids.close()
    g = GenICamCamera(device="IDS_U3-386xCP-M_1")
    g.open()
    assert len(hwlock.held()) == 1
    g.close()
    assert hwlock.held() == []


def test_failed_genicam_open_releases(fake_harvesters):
    fake_harvesters["fail"] = True
    g = GenICamCamera(device="0")
    with pytest.raises(RuntimeError, match="start failed"):
        g.open()
    assert hwlock.held() == []


# --------------------------------------------------------------------------- #
# fake pylablib (own-KCube Z fallback)
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_pylablib(monkeypatch):
    state = {"fail": False, "opened": []}

    class KinesisPiezoController:
        def __init__(self, serial):
            if state["fail"]:
                raise RuntimeError("fake: Kinesis open failed")
            state["opened"].append(serial)

        def close(self):
            pass

    thorlabs = types.SimpleNamespace(KinesisPiezoController=KinesisPiezoController)
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = thorlabs
    monkeypatch.setitem(sys.modules, "pylablib", types.ModuleType("pylablib"))
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    return state


def test_kcube_claims_serial_conflicts_and_releases(fake_pylablib):
    a = KCubeZFocus(serial="29250001")
    a.open()
    b = KCubeZFocus(serial=" 29250001 ")    # (b) same box, written differently
    with pytest.raises(hwlock.HardwareBusy, match="camera"):
        b.open()
    assert fake_pylablib["opened"] == ["29250001"]  # the second never reached it
    a.close()
    assert hwlock.held() == []
    b.open()                                  # (c) released -> succeeds
    b.close()


def test_kcube_failed_open_and_empty_serial_leave_nothing(fake_pylablib):
    fake_pylablib["fail"] = True
    with pytest.raises(RuntimeError, match="Kinesis open failed"):
        KCubeZFocus(serial="29250001").open()
    assert hwlock.held() == []
    with pytest.raises(RuntimeError, match="kcube_serial"):
        KCubeZFocus(serial="").open()
    assert hwlock.held() == []


# --------------------------------------------------------------------------- #
# the simulator claims nothing
# --------------------------------------------------------------------------- #
def test_sim_claims_nothing():
    from camera.sim_system import build_sim_system

    brain, *_ = build_sim_system()
    brain.start()
    try:
        assert hwlock.held() == []
    finally:
        brain.shutdown()
    assert hwlock.held() == []


# --------------------------------------------------------------------------- #
# the service: a busy camera ends startup with ONE line and a non-zero exit
# --------------------------------------------------------------------------- #
def test_run_service_busy_camera_is_one_clean_line(fake_ids, monkeypatch, capsys):
    import importlib.util

    calls, _ = fake_ids
    holder = hwlock.claim(camera_address(SERIAL), "othercam")   # someone else has it
    try:
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_service.py"
        spec = importlib.util.spec_from_file_location("camera_run_service_t", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.delenv("AALTOFLOW_ENDPOINTS", raising=False)
        monkeypatch.delenv("TRMOKE_ENDPOINTS", raising=False)
        monkeypatch.setattr(sys, "argv", ["run_service.py", "--real",
                                          "--cmd-port", "15971", "--pub-port", "15972"])
        calls.clear()
        rc = mod.main()
        err = capsys.readouterr().err
        assert rc == 4
        assert "already in use by othercam" in err
        assert "Traceback" not in err and len(err.strip().splitlines()) == 1
        # it never opened the camera, so it sent the camera nothing on the way out
        assert not [c for c in calls if c[0] in ("open_device", "execute", "stop_acq")]
    finally:
        holder.release()
