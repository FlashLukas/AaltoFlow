"""Service <-> client over real sockets, on NON-default ports (15960+) so the
tests never collide with a service running in the lab -- nor with
mag2d-control's own tests, which use 15950-15957.

These run the simulated magnet on the REAL clock (a service has threads), so
the targets are kept small to stay quick.
"""

import time

import pytest

pytest.importorskip("zmq")

from mag2dcal.calibration import AxisCalibration, Calibration
from mag2dcal.config import Config
from mag2dcal.controller import Refused
from mag2dcal.net.client import Mag2dcalClient
from mag2dcal.net.protocol import CONTRACT_STATUS_KEYS, EXTRA_STATUS_KEYS
from mag2dcal.net.service import Mag2dcalService
from mag2dcal.sim_system import build_sim_system


@pytest.fixture
def pair(request, tmp_path):
    port = request.param if hasattr(request, "param") else 15960
    cfg = Config()
    cfg.calibration.directory = str(tmp_path)
    cfg.calibration.load_newest_on_start = False
    ctrl, sim = build_sim_system(cfg, seed=4)
    svc = Mag2dcalService(ctrl, host="127.0.0.1", cmd_port=port, pub_port=port + 1)
    svc.start()
    client = Mag2dcalClient(host="127.0.0.1", cmd_port=port, pub_port=port + 1)
    try:
        yield cfg, ctrl, sim, svc, client
    finally:
        client.shutdown()
        svc.stop()


def _wait(pred, timeout_s=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pred():
            return True
        time.sleep(0.03)
    return False


def _toy_axis(gain=20.0, v=5.0, n=5, h=0.4):
    up = [(-v + 2 * v * i / (n - 1), gain * (-v + 2 * v * i / (n - 1)) + h)
          for i in range(n)]
    return AxisCalibration(up=up, down=[(V, B - 2 * h) for V, B in up])


@pytest.mark.parametrize("pair", [15960], indirect=True)
def test_status_has_every_contract_key_on_both_paths(pair):
    cfg, ctrl, sim, svc, client = pair
    info = client.start()
    assert info["field_max_mT"] == cfg.limits.field_max_mT
    assert info["freeze_enabled"] is True and info["calibrated"] is False
    req = client._cmd({"cmd": "status"})["status"]
    assert _wait(lambda: bool(client._latest))
    pub = dict(client._latest)
    for key in CONTRACT_STATUS_KEYS + EXTRA_STATUS_KEYS:
        assert key in req, f"REQ status lacks {key}"
        assert key in pub, f"PUB status lacks {key}"
    assert set(req) == set(pub)
    assert len(req["output_V"]) == 2 and len(req["temp_C"]) == 2 and len(req["hall_V"]) == 2


@pytest.mark.parametrize("pair", [15962], indirect=True)
def test_set_field_blocking_over_the_wire(pair):
    cfg, ctrl, sim, svc, client = pair
    client.start()
    st = client.set_field_blocking(20.0, 30.0, timeout_s=30.0)
    assert st.field_stable and st.setpoint_field_mT == 20.0 and st.setpoint_angle_deg == 30.0
    assert abs(st.measured_magnitude_mT - 20.0) < 1.0
    assert st.frozen                                  # arrived and stopped pushing
    # straight into the next point: the stale "stable" must not fool the helper
    st = client.set_field_blocking(25.0, timeout_s=30.0)
    assert st.setpoint_field_mT == 25.0 and abs(st.measured_magnitude_mT - 25.0) < 1.0

    client.set_bx(3.0)
    assert _wait(lambda: client.status().setpoint_bx_mT == 3.0)


@pytest.mark.parametrize("pair", [15964], indirect=True)
def test_refusal_bad_command_config_and_describe(pair):
    cfg, ctrl, sim, svc, client = pair
    client.start()

    # a malformed command is ok:false, and the service survives
    assert client._cmd({"cmd": "set_field"})["ok"] is False
    assert client._cmd({"cmd": "no_such_verb"})["ok"] is False
    assert client._cmd({"cmd": "info"})["ok"] is True

    # config round trip, applied in place on the service
    client.cfg.control.tolerance_mT = 0.25
    client.cfg.interlock.temp_monitor = True
    client.cfg.stabilizer.deadband_mT = 0.1
    client.apply_config()
    assert ctrl.cfg.control.tolerance_mT == 0.25 and ctrl.cfg.interlock.temp_monitor is True
    assert ctrl.cfg.stabilizer.deadband_mT == 0.1
    assert client.get_config().control.tolerance_mT == 0.25

    m = client.describe()
    assert m["module"] == "mag2dcal" and any(p["id"] == "field" for p in m["parameters"])

    # water lost -> FAULT; a setter over the wire raises Refused with the reason
    sim.p.water_ok = False
    assert _wait(lambda: client.status().state == "FAULT")
    with pytest.raises(Refused) as exc:
        client.set_field(10.0)
    assert "FAULT" in str(exc.value)
    with pytest.raises(Refused):
        client.clear_fault()
    sim.p.water_ok = True
    assert _wait(lambda: client.status().water_ok)
    client.clear_fault()
    assert _wait(lambda: client.status().state in ("OFF", "RAMP_DOWN"))


@pytest.mark.parametrize("pair", [15966], indirect=True)
def test_calibration_and_stabilizer_over_the_wire(pair):
    cfg, ctrl, sim, svc, client = pair
    client.start()
    assert client.get_calibration() is None
    before = client.describe()["revision"]

    cal = Calibration(axes=[_toy_axis(20.0), _toy_axis(19.4)], note="over the wire")
    client.set_calibration(cal)
    back = client.get_calibration()
    assert back is not None and back.to_dict() == cal.to_dict()
    assert client.status().calibrated is True
    # the limits followed it, so the manifest changed and clients re-fetch
    assert client.describe()["revision"] != before
    assert client.field_envelope_mT() == pytest.approx(ctrl.field_envelope_mT())
    assert client.field_envelope_mT() < cfg.limits.field_max_mT

    client.set_calibration(None)
    assert client.get_calibration() is None
    assert _wait(lambda: client.status().calibrated is False)

    client.set_stabilizer(False)
    assert _wait(lambda: client.status().stabilizer is False)
    client.set_stabilizer(True)
    assert _wait(lambda: client.status().stabilizer is True)


@pytest.mark.parametrize("pair", [15968], indirect=True)
def test_calibrate_runs_in_the_service_and_can_be_aborted(pair):
    cfg, ctrl, sim, svc, client = pair
    client.start()
    client.calibrate(n_per_leg=5, dwell_s=0.05, v_max=2.0)
    assert _wait(lambda: client.status().state == "CALIBRATE")
    assert _wait(lambda: client.status().calibration_progress > 0.0)
    with pytest.raises(Refused):
        client.set_field(10.0)                 # no setpoints during a sweep
    client.zero()                              # the abort
    assert _wait(lambda: client.status().state != "CALIBRATE")
    assert client.status().setpoint_field_mT == 0.0


@pytest.mark.parametrize("pair", [15970], indirect=True)
def test_shutdown_verb_stops_the_service(pair):
    cfg, ctrl, sim, svc, client = pair
    client.start()
    assert client.stop_service() == {"ok": True, "stopping": True}
    assert _wait(lambda: svc._stop.is_set(), 2.0)
