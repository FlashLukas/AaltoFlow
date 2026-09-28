"""Deep cleaning 2026-09-28: one test per bug found, each written to FAIL first.

Offline, non-default ports (157xx), no hardware.  The KCube tests use a fake
``pylablib.devices.Thorlabs`` that copies pylablib's own volt <-> device-unit
arithmetic (pylablib/devices/Thorlabs/kinesis.py, ``_pzctl_voltage_u2d`` /
``_pzctl_voltage_d2u``), so the quantisation is the real library's, not a guess.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import zmq

from zpiezo.backends.kcube import KCubeZ
from zpiezo.backends.sim import SimZ
from zpiezo.config import Config
from zpiezo.net.client import ZPiezoClient
from zpiezo.net.describe import build_manifest
from zpiezo.net.service import ZPiezoService
from zpiezo.sim_system import build_sim_system
from zpiezo.zpiezo import ZPiezo


# --------------------------------------------------------------------------- #
# 1. Two threads talking to the KCube at once
# --------------------------------------------------------------------------- #
class OverlapZ(SimZ):
    """A sim KCube that records whether two calls were ever inside it at once.

    On the real KCube every pylablib call is several send/receive pairs on
    one USB serial link with no lock of its own (``BasicKinesisDevice.query``
    = send_comm + recv_comm), so two overlapping calls can swap replies.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._inside = 0
        self._guard = threading.Lock()
        self.overlaps = 0

    def _enter(self):
        with self._guard:
            self._inside += 1
            if self._inside > 1:
                self.overlaps += 1
        time.sleep(0.001)            # a USB round trip takes time

    def _leave(self):
        with self._guard:
            self._inside -= 1

    def set_voltage(self, volts):
        self._enter()
        try:
            super().set_voltage(volts)
        finally:
            self._leave()

    def read_voltage(self):
        self._enter()
        try:
            return super().read_voltage()
        finally:
            self._leave()


def test_backend_is_never_called_from_two_threads_at_once():
    be = OverlapZ(v0=10.0)
    brain = ZPiezo(be, Config())
    brain.start()
    stop = threading.Event()

    def publisher():                 # what the z-pub thread does at 8 Hz
        while not stop.is_set():
            brain.status()

    t = threading.Thread(target=publisher, daemon=True)
    t.start()
    try:
        for i in range(150):         # what the z-cmd thread does on set_voltage
            brain.set_voltage(10.0 + (i % 5))
            brain.read_voltage()
    finally:
        stop.set()
        t.join(2)
    assert be.overlaps == 0, f"{be.overlaps} overlapping backend calls"


# --------------------------------------------------------------------------- #
# 2. Limits reported by status/info after a set_config
# --------------------------------------------------------------------------- #
def test_status_and_info_limits_follow_set_config():
    """The camera's RemoteZFocus sizes its autofocus sweep from `info`."""
    brain, _ = build_sim_system(Config(), v0=10.0)
    svc = ZPiezoService(brain, host="127.0.0.1", cmd_port=15701, pub_port=15702)
    svc.start()
    try:
        assert svc._dispatch({"cmd": "set_config",
                              "config": {"limits": {"v_max": 50.0}}})["ok"]
        info = svc._dispatch({"cmd": "info"})["info"]["limits"]
        st = svc._dispatch({"cmd": "status"})["status"]
        man = {p["id"]: p for p in build_manifest(brain)["parameters"]}
        assert info["v_max"] == 50.0
        assert st["v_max"] == 50.0
        assert man["voltage"]["max"] == 50.0
    finally:
        svc.stop()


# --------------------------------------------------------------------------- #
# 3. describe's settle tolerance vs the KPZ101's resolution
# --------------------------------------------------------------------------- #
class QuantisingController:
    """pylablib KinesisPiezoController, reduced to its voltage arithmetic."""

    range_v = 75

    def __init__(self, serial):
        self.serial = serial
        self._d = 0                  # device units, 0..32767 = 0..range

    def _u2d(self, v):               # copied from pylablib: int(), i.e. TRUNCATES
        return max(-2**15, min(int(v / self.range_v * (2**15 - 1)), 2**15 - 1))

    def _d2u(self, d):
        return d / (2**15 - 1) * self.range_v

    def set_output_voltage(self, v):
        self._d = self._u2d(v)
        return self._d2u(self._d)

    def get_output_voltage(self):
        return self._d2u(self._d)

    def close(self):
        pass


@pytest.fixture
def fake_pylablib(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    thorlabs = types.SimpleNamespace(KinesisPiezoController=QuantisingController,
                                     list_kinesis_devices=lambda: [])
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = thorlabs
    root = types.ModuleType("pylablib")
    root.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", root)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    yield


@pytest.mark.parametrize("range_v", [75, 100, 150])
def test_settle_tolerance_covers_the_kpz101_resolution(fake_pylablib, range_v):
    """A scan on `voltage` waits until status echoes the value it sent.

    If the tolerance is finer than one device step, most setpoints can never
    be echoed and every scan point runs into scan-core's settle timeout.
    """
    QuantisingController.range_v = range_v
    cfg = Config()
    brain = ZPiezo(KCubeZ("29500123", 0.0, 75.0), cfg)
    brain.start()
    try:
        tol = {p["id"]: p for p in build_manifest(brain)["parameters"]}[
            "voltage"]["settle"]["tol"]
        worst = 0.0
        for i in range(0, 751):
            target = i * 0.1
            brain.set_voltage(target)
            worst = max(worst, abs(brain.status().voltage - target))
        assert worst <= tol, f"echo error {worst*1e3:.2f} mV > tol {tol*1e3:.2f} mV"
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 4. set_config with a bad envelope or text values
# --------------------------------------------------------------------------- #
def test_inverted_envelope_is_refused_and_nothing_moves():
    """v_min > v_max used to be accepted: the clamp then returned v_max for
    every request, and apply_config drove the focus straight to it."""
    brain, be = build_sim_system(Config(), v0=12.3)
    svc = ZPiezoService(brain, host="127.0.0.1", cmd_port=15703, pub_port=15704)
    svc.start()
    cli = ZPiezoClient("127.0.0.1", 15703, 15704)
    try:
        # over the wire: the reply must be {"ok": false, ...} (client raises)
        with pytest.raises(RuntimeError, match="v_min < v_max"):
            cli.set_config({"limits": {"v_min": 80.0}})
        assert brain.cfg.limits.v_min == 0.0          # config untouched
        assert be.read_voltage() == pytest.approx(12.3)  # focus untouched
    finally:
        cli.close()
        svc.stop()


def test_set_config_values_are_cast_to_the_field_type():
    """A text value must not poison the config (gotcha #3 over the wire):
    "50" as v_max made every later set_voltage fail with a TypeError."""
    brain, _ = build_sim_system(Config(), v0=12.3)
    brain.start()
    try:
        brain.set_config({"limits": {"v_max": "50", "enforce": "False"}})
        assert brain.cfg.limits.v_max == 50.0
        assert brain.cfg.limits.enforce is False
        brain.cfg.limits.enforce = True
        assert brain.set_voltage(60) == 50.0
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 5. A port that is already taken
# --------------------------------------------------------------------------- #
def test_port_in_use_fails_start_before_the_kcube_is_opened():
    """The sockets used to be bound inside the threads: a clash killed the
    thread silently and the service ran on, deaf, holding the KCube."""
    ctx = zmq.Context.instance()
    squatter = ctx.socket(zmq.REP)
    squatter.bind("tcp://127.0.0.1:15705")
    be = OverlapZ(v0=12.3)
    opened = []
    be.open = lambda: opened.append(True)
    svc = ZPiezoService(ZPiezo(be, Config()), host="127.0.0.1",
                        cmd_port=15705, pub_port=15706)
    try:
        with pytest.raises(RuntimeError):
            svc.start()
        assert opened == []
    finally:
        svc.stop()
        squatter.close(0)


# --------------------------------------------------------------------------- #
# 6. The raw console
# --------------------------------------------------------------------------- #
def _console():
    path = Path(__file__).resolve().parents[1] / "scripts" / "zpiezo_console.py"
    spec = importlib.util.spec_from_file_location("zpiezo_console_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("verb", ["describe", "shutdown"])
def test_console_knows_the_universal_verbs(verb):
    assert _console().build_request(verb) == {"cmd": verb}


# --------------------------------------------------------------------------- #
# 7. A non-number slipping past the clamp
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [float("nan"), "nan"])
def test_nan_voltage_is_refused(bad):
    """min(max(nan, lo), hi) is nan: the clamp let it through to the KCube.
    (The console's `set_voltage nan` sends exactly this; Python's json
    carries NaN.)"""
    brain, be = build_sim_system(Config(), v0=12.3)
    brain.start()
    try:
        with pytest.raises(ValueError):
            brain.set_voltage(bad)
        assert be.read_voltage() == pytest.approx(12.3)
        assert brain.status().target == pytest.approx(12.3)
    finally:
        brain.shutdown()
