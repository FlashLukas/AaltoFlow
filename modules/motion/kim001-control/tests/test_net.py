"""Service <-> client round-trip on NON-default ports (section 9).

Non-default ports (19440/19441) so this never collides with a real service.
Covers the two-language remote surface on the focus axis Z: read/set the
position in steps AND in micrometres, the velocity/calibration bridge, and the
refusal of X / Y over the wire.
"""

import time

import pytest

from kim001.config import Config
from kim001.net.client import Kim001Client
from kim001.net.service import Kim001Service
from kim001.sim_system import build_sim_system

CMD, PUB = 19440, 19441
Z = 2


@pytest.fixture()
def service_and_client():
    cfg = Config()
    cfg.calibration.um_per_step_z = 0.02
    brain, _ = build_sim_system(cfg)
    svc = Kim001Service(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = Kim001Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    time.sleep(0.2)  # let PUB warm up
    try:
        yield brain, cli
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)


def _wait(cli, pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred(cli.status()):
            return True
        time.sleep(0.02)
    return False


def test_x_and_y_commands_are_refused_over_the_wire(service_and_client):
    """The wire contract: {"ok": false, "error": "... not fitted on a KIM001 ..."}
    for every X / Y command -- the client raises it as RuntimeError."""
    brain, cli = service_and_client
    for axis in ("X", "Y", 0, 1):
        for call in (lambda a: cli.move_to_um(a, 1.0),
                     lambda a: cli.move_to_step(a, 10),
                     lambda a: cli.move_relative_um(a, 1.0),
                     lambda a: cli.zero_counter(a),
                     lambda a: cli.set_calibration(a, 0.03, 1),
                     lambda a: cli.stop(a)):
            with pytest.raises(RuntimeError, match="not fitted on a KIM001"):
                call(axis)
    with pytest.raises(RuntimeError, match="not fitted on a KIM001"):
        cli.move_image_px(10, 0)
    assert cli.get_px_calibration() is None
    st = cli.status()
    assert st.position_steps[:2] == [0, 0] and st.moving[:2] == [False, False]
    assert st.fitted == [False, False, True]


def test_info_and_config(service_and_client):
    _brain, cli = service_and_client
    info = cli.info()
    assert info["axes"] == ["X", "Y", "Z"]
    assert info["fitted"] == ["Z"]
    assert "calibration" in info
    cfg = cli.get_config()
    assert "limits" in cfg and "calibration" in cfg and "motion" in cfg
    # UI theme travels over the wire and round-trips through set_config
    assert cfg["ui"]["theme"] in ("dark", "light")
    cli.set_config({"ui": {"theme": "light"}})
    assert cli.get_config()["ui"]["theme"] == "light"


def test_step_move_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate(Z, 2000)
    target = cli.move_to_step("Z", 5000)   # accepts axis by name
    assert target == 5000
    assert _wait(cli, lambda s: s.position_steps[Z] == 5000 and not s.moving[Z])


def test_um_move_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate("Z", 2000)
    target = cli.move_to_um("Z", 100.0)    # 100 um / 0.02 = 5000 steps
    assert target == 5000
    assert _wait(cli, lambda s: abs(s.position_um[Z] - 100.0) < 1e-6 and not s.moving[Z])


def test_relative_um_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate("Z", 2000)
    cli.move_to_um("Z", 20.0)
    assert _wait(cli, lambda s: s.position_steps[Z] == 1000 and not s.moving[Z])
    cli.move_relative_um("Z", 10.0)        # +500 steps
    assert _wait(cli, lambda s: s.position_steps[Z] == 1500 and not s.moving[Z])


def test_velocity_um_clamped_over_wire(service_and_client):
    _brain, cli = service_and_client
    actual = cli.set_velocity_um("Z", 100.0)  # -> 5000 steps/s clamps to 2000 = 40 um/s
    assert abs(actual - 40.0) < 1e-6
    assert _wait(cli, lambda s: abs(s.step_rate[Z] - 2000.0) < 1e-6)


def test_move_clamped_over_wire(service_and_client):
    _brain, cli = service_and_client
    target = cli.move_to_step("Z", 9_999_999)   # max default 650,000 (13 mm)
    assert target == 650_000
    cli.stop("Z")


def test_datum_and_display_zero_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate("Z", 2000)
    cli.move_to_step("Z", 3000)
    assert _wait(cli, lambda s: s.position_steps[Z] == 3000 and not s.moving[Z])

    origins = cli.set_zero("Z")
    assert origins[0] == 3000
    assert _wait(cli, lambda s: s.rel_steps[Z] == 0)

    cli.zero_counter("Z")
    assert _wait(cli, lambda s: s.position_steps[Z] == 0)


def test_calibration_error_over_wire(service_and_client):
    _brain, cli = service_and_client
    with pytest.raises(RuntimeError):   # {"ok": false} -> client raises
        cli.set_calibration("Z", 0.0)


def test_set_leash_over_wire(service_and_client):
    _brain, cli = service_and_client
    state = cli.set_leash(enabled=True, leash_z=400)
    assert state["enabled"] and state["leash_z"] == 400
    assert _wait(cli, lambda s: s.leash and s.limit_hi[Z] == 400 and s.limit_hi[0] == 0)
    # a move now clamps to the leash box, not the absolute limit
    assert cli.move_to_step("Z", -99999) == -400


def test_presets_over_wire(service_and_client):
    _brain, cli = service_and_client
    s = cli.set_speed(False)
    assert s["fast"] is False
    assert _wait(cli, lambda st: st.speed_fast is False and abs(st.step_rate[Z] - 300.0) < 1e-6)
    st = cli.set_step_size(True)
    assert st["large"] is True
    assert _wait(cli, lambda s2: s2.step_large and abs(s2.voltage[Z] - 125.0) < 1e-6)


def test_position_list_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate("Z", 2000)
    cli.move_to_step("Z", 200)
    # wait for the POSITION, not just "not moving": a cached frame from before
    # the move also says "not moving" (gotcha #2)
    assert _wait(cli, lambda s: s.position_steps[Z] == 200 and not s.moving[Z])
    cli.store_position(0, "wire")
    positions = cli.get_positions()
    assert positions[0]["used"] and positions[0]["name"] == "wire"
    assert int(positions[0]["z"]) == 200
