"""Lukas's rule (2026-09-27): starting the service READS the instrument and
changes nothing.

Two layers are checked:
  * the BRAIN: a recording wrapper around the simulator fails on any
    state-changing call during start();
  * the REAL backend: ScuStage.open() against a fake SmarAct DLL that fails
    on any SA_* function that would change the controller.
And the adoption itself: a controller left in a non-default state (fast
speed, already referenced, carriage elsewhere) shows up as such in status.
"""

import pytest

from helpers import fast_cfg, wait_until
from smaract.backends import scu as scu_mod
from smaract.backends.sim import SimScu
from smaract.config import Config
from smaract.smaract import Positioner

#: Backend methods that change the controller's state or move the carriage.
WRITES = ("move_absolute", "move_relative", "find_reference", "stop",
          "set_max_frequency")


class RecordingSim(SimScu):
    """The simulator, but every state-changing call is recorded while
    ``armed`` -- so a test can say "no writes during start()"."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.armed = True
        self.writes = []

    def __getattribute__(self, name):
        if name in WRITES and object.__getattribute__(self, "armed"):
            object.__getattribute__(self, "writes").append(name)
        return object.__getattribute__(self, name)


def test_start_issues_no_state_changing_calls():
    cfg = Config()                      # config asks for 2 mm/s ...
    backend = RecordingSim(cfg, freq_hz=3500, power_on_mm=12.0)  # ... SCU runs 3.5
    brain = Positioner(backend, cfg)
    brain.start()
    try:
        wait_until(lambda: False, 0.2)  # let the poll thread run a few cycles
        assert backend.writes == [], f"start() wrote to the controller: {backend.writes}"
        assert backend.get_max_frequency() == 3500
    finally:
        backend.armed = False           # shutdown's STOP is allowed (not part of the rule)
        brain.shutdown()


def test_status_after_start_reflects_preexisting_state():
    cfg = Config()                      # defaults: 2 mm/s, NOT referenced
    backend = SimScu(cfg, freq_hz=3500, referenced=True, power_on_mm=42.3)
    brain = Positioner(backend, cfg)
    brain.start()
    try:
        st = brain.status()
        assert st.referenced                                   # adopted, not searched
        assert st.ref_id == 0 and not st.referencing
        assert st.max_frequency_hz == 3500
        assert st.velocity_mm_s == pytest.approx(3.5)          # 3500 Hz x 1 um
        assert cfg.motion.velocity_mm_s == pytest.approx(3.5)  # config follows the SCU
        assert st.position_mm == pytest.approx(42.3, abs=2e-4)
        assert st.target_mm == pytest.approx(st.position_mm)   # nothing pending
        assert not st.moving and st.on_target
        # referenced at start -> absolute moves are allowed straight away
        brain.move_to(42.0)
    finally:
        brain.shutdown()


def test_set_config_of_another_group_does_not_rewrite_adopted_speed():
    cfg = fast_cfg()
    backend = RecordingSim(cfg, freq_hz=12345)
    brain = Positioner(backend, cfg)
    brain.start()
    try:
        cfg.ui.theme = "light"
        brain.apply_config()            # what set_config does after editing cfg
        assert backend.writes == []
        # ... but an explicit speed change IS written
        cfg.motion.velocity_mm_s = 5.0
        brain.apply_config()
        assert backend.writes == ["set_max_frequency"]
        assert backend.get_max_frequency() == 5000
    finally:
        backend.armed = False
        brain.shutdown()


def test_out_of_range_speed_is_adopted_not_corrected():
    cfg = Config()                      # max_velocity 10 mm/s
    events = []
    backend = RecordingSim(cfg, freq_hz=15000)   # 15 mm/s, above the config limit
    brain = Positioner(backend, cfg)
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    try:
        assert brain.status().velocity_mm_s == pytest.approx(15.0)
        assert backend.writes == []
        assert any(lvl == "warn" and "outside" in msg for lvl, msg in events)
    finally:
        backend.armed = False
        brain.shutdown()


# --------------------------------------------------------------------------- #
# the real backend against a fake DLL
# --------------------------------------------------------------------------- #
#: SA_* functions that only READ (or open/close the library connection).
READS = {"SA_InitDevices", "SA_ReleaseDevices", "SA_GetNumberOfDevices",
         "SA_GetDeviceID", "SA_GetDeviceFirmwareVersion", "SA_GetSensorPresent_S",
         "SA_GetSensorType_S", "SA_GetStatus_S", "SA_GetPosition_S",
         "SA_GetPhysicalPositionKnown_S", "SA_GetClosedLoopMaxFrequency_S"}


class FakeFn:
    def __init__(self, lib, name):
        self.lib, self.name = lib, name
        self.argtypes, self.restype = None, None

    def __call__(self, *args):
        self.lib.calls.append(self.name)
        if self.name not in READS:
            raise AssertionError(f"{self.name} changes the controller")
        out = {"SA_GetNumberOfDevices": 1, "SA_GetSensorPresent_S": 1,
               "SA_GetSensorType_S": self.lib.sensor_type,
               "SA_GetClosedLoopMaxFrequency_S": 4200,
               "SA_GetPosition_S": 123456, "SA_GetStatus_S": 4,
               "SA_GetPhysicalPositionKnown_S": 1}.get(self.name)
        if out is not None:
            args[-1]._obj.value = out   # ctypes.byref(...) -> the c_uint/c_int
        return 0


class FakeLib:
    def __init__(self, sensor_type=0):
        self.calls, self.sensor_type = [], sensor_type
        self._fns = {}

    def __getattr__(self, name):
        if not name.startswith("SA_"):
            raise AttributeError(name)
        return self.__dict__["_fns"].setdefault(name, FakeFn(self, name))


@pytest.mark.parametrize("sensor_type", [0, 7])
def test_real_backend_open_only_reads(monkeypatch, sensor_type):
    lib = FakeLib(sensor_type=sensor_type)
    monkeypatch.setattr(scu_mod.ctypes, "CDLL", lambda path: lib)
    cfg = Config()
    cfg.hardware.sensor_type = sensor_type
    stage = scu_mod.ScuStage(cfg)
    stage.open()
    assert stage.get_max_frequency() == 4200
    assert stage.read_position_mm() == pytest.approx(12.3456)
    assert stage.channel_state() == "holding"
    assert stage.physical_position_known()
    stage.close()
    assert set(lib.calls) <= READS
    assert ("SA_GetSensorType_S" in lib.calls) == bool(sensor_type)


def test_real_backend_refuses_wrong_sensor_type_without_writing(monkeypatch):
    lib = FakeLib(sensor_type=3)
    monkeypatch.setattr(scu_mod.ctypes, "CDLL", lambda path: lib)
    cfg = Config()
    cfg.hardware.sensor_type = 7
    with pytest.raises(scu_mod.ScuError, match="sensor type 3"):
        scu_mod.ScuStage(cfg).open()
    assert set(lib.calls) <= READS
