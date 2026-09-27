"""Network integration test: drive the controller through the ZeroMQ service.

Starts a service in-process on loopback, connects a client, and seeks a field
entirely over the sockets -- the same path the GUI uses in --connect mode.
"""

import time

import pytest

pytest.importorskip("zmq")

from clMag.config import Config
from clMag.sim_system import build_sim_system
from clMag.net.service import ClMagService
from clMag.net.client import ClMagClient


def _wait(client, predicate, timeout_s):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if predicate(client.status()):
            return True
        time.sleep(0.03)
    return False


def test_network_seek_and_bad_command():
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5765, pub_port=5766)
    svc.start()
    client = None
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5765, pub_port=5766)
        info = client.start()
        assert info["n_points"] > 0
        assert info["current_max"] == cfg.limits.current_max_A

        # seek a field over the network
        client.set_field(40.0)
        assert _wait(client, lambda s: s.field_stable, 12.0), "never stabilised over the network"
        assert abs(client.status().measured_field_mT - 40.0) <= cfg.limits.field_tolerance_mT + 1e-6

        # a malformed command must come back as ok=False, not crash the service
        reply = client._cmd({"cmd": "set_field"})       # missing field_mT
        assert reply["ok"] is False
        # ... and the service is still alive afterwards
        assert client._cmd({"cmd": "info"})["ok"] is True
    finally:
        if client:
            client.shutdown()
        svc.stop()


def test_remote_config_and_calibration():
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5767, pub_port=5768)
    svc.start()
    client = None
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5767, pub_port=5768)
        client.start()

        # get_config populated the client's cfg from the service
        assert client.cfg.pid.Kc_A_per_mT == ctrl.cfg.pid.Kc_A_per_mT

        # edit on the client, push, and confirm the service applied + re-tuned
        client.cfg.pid.Kc_A_per_mT = 0.033
        client.cfg.limits.field_tolerance_mT = 0.2
        client.apply_config()
        time.sleep(0.2)
        assert abs(ctrl.pi.Kc - 0.033) < 1e-9
        assert ctrl.cfg.limits.field_tolerance_mT == 0.2

        # fetch the calibration curve over the wire
        cal = client.get_calibration()
        assert cal is not None and len(cal.currents_A) > 0

        # push a replacement curve and confirm the service took it
        from clMag.calibration import FieldCalibration
        small = FieldCalibration(currents_A=[-1.0, 0.0, 1.0],
                                 fields_mT=[-30.0, 0.0, 30.0], hall=cal.hall)
        client.set_calibration(small)
        time.sleep(0.2)
        assert len(ctrl.calibration.currents_A) == 3
    finally:
        if client:
            client.shutdown()
        svc.stop()


def test_remote_aux_io():
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5769, pub_port=5770)
    svc.start()
    client = None
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5769, pub_port=5770)
        client.start()

        # analog output: set over the wire, read back in the status snapshot
        client.aux_set_ao("Dev1/ao0", 2.5)
        client.aux_set_ao("Dev1/ao1", -9.0)
        time.sleep(0.25)
        aux = client.status().aux
        assert abs(aux["ao"]["Dev1/ao0"] - 2.5) < 1e-9
        assert abs(aux["ao"]["Dev1/ao1"] + 9.0) < 1e-9

        # over-range AO clamps to ±10 V
        client.aux_set_ao("Dev1/ao2", 50.0)
        time.sleep(0.25)
        assert client.status().aux["ao"]["Dev1/ao2"] == 10.0

        # digital output toggles and shows up in status
        client.aux_set_do("Dev1/port0/line1", True)
        time.sleep(0.25)
        assert client.status().aux["do"]["Dev1/port0/line1"] is True

        # single analog-input read returns a plausible ±10 V value
        v = client.aux_read_ai("Dev1/ai1")
        assert -10.0 <= v <= 10.0
    finally:
        if client:
            client.shutdown()
        svc.stop()


def test_set_field_blocking_settles_and_guards_stale_status():
    """The blocking helper must not be fooled by the PREVIOUS point's status.

    This is the whole reason `wait_stable` takes a target. Commands are
    fire-and-forget, so right after `set_field(10)` the service is still
    reporting the settled 40 mT point -- `field_stable` is True, but for the
    wrong field. Without the setpoint-adoption guard the second call here would
    return immediately and `measured` would still be ~40 mT.
    """
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5771, pub_port=5772)
    svc.start()
    client = None
    tol = cfg.limits.field_tolerance_mT
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5771, pub_port=5772)
        client.start()

        st = client.set_field_blocking(40.0, timeout_s=20.0)
        assert st.field_stable
        assert st.setpoint_field_mT == 40.0
        assert abs(st.measured_field_mT - 40.0) <= tol + 1e-6

        # straight into a second point, with no wait in between -- the stale
        # status from the first point is live at this moment.
        st = client.set_field_blocking(10.0, timeout_s=20.0)
        assert st.setpoint_field_mT == 10.0
        assert abs(st.measured_field_mT - 10.0) <= tol + 1e-6, \
            "returned while the field was still at the previous point"
    finally:
        if client:
            client.shutdown()
        svc.stop()


def test_wait_stable_raises_on_timeout():
    """A timeout must raise, not return a falsy flag that a caller can ignore.

    A scan that quietly records unsettled points yields data that looks fine and
    is wrong, so this failure has to be loud.
    """
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5773, pub_port=5774)
    svc.start()
    client = None
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5773, pub_port=5774)
        client.start()

        client.set_field(40.0)
        with pytest.raises(TimeoutError) as exc:
            client.wait_stable(40.0, timeout_s=0.2)
        # the message has to say enough to debug it without a rerun
        assert "40" in str(exc.value)
        assert "state=" in str(exc.value)

        # the client is still usable after a timeout
        assert client.wait_stable(40.0, timeout_s=20.0).field_stable
    finally:
        if client:
            client.shutdown()
        svc.stop()


def test_wait_idle_after_demag():
    """demag has no setpoint to adopt, so `finished` = the state machine came home."""
    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5775, pub_port=5776)
    svc.start()
    client = None
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5775, pub_port=5776)
        client.start()
        client.set_field_blocking(30.0, timeout_s=20.0)

        client.demag(1.0)
        st = client.wait_idle(timeout_s=40.0)
        assert st.state == "IDLE"
        assert abs(st.measured_field_mT) < 5.0, "demag should leave the field near zero"
    finally:
        if client:
            client.shutdown()
        svc.stop()
