"""Calibrate stage + the stabiliser's wrong-way guard (Blue Box, 2026-11-11).

On the Blue Box xy_map was inverted on both axes ("+X-Y" where "-X+Y" was
right): the stabiliser walked the sample AWAY from the laser, and nothing
said why. Now the camera can MEASURE xy_map (move each stage axis both ways,
watch the pattern), and the stabiliser stops itself when two corrections in a
row make the distance larger.

The simulator's stage is aligned with the image (+stage x -> +image x), so the
true map there is "+X+Y".
"""

from __future__ import annotations

import time

import pytest

from camera.config import Config, xy_map_from_moves
from camera.sim_system import build_sim_system


def _wait(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def tracked():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.stabilizer.settle_s = 0.05
    brain, cam, xy, z = build_sim_system(cfg)
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(10)
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: brain.status().match_found)
    brain.events = events
    yield brain
    brain.shutdown()


def test_the_map_from_measured_shifts():
    # Blue Box numbers: +X moved the image LEFT, +Y moved it DOWN -> "-X+Y"
    assert xy_map_from_moves((-28.3, -2.0), (-0.1, 34.8)) == "-X+Y"
    assert xy_map_from_moves((5.0, 0.2), (0.1, 5.0)) == "+X+Y"
    # a stage mounted at 90 deg: +X moves the image up, +Y moves it right
    assert xy_map_from_moves((0.1, -5.0), (5.0, 0.0)) == "+Y-X"


def test_calibrate_stage_finds_the_true_map_and_puts_the_stage_back(tracked):
    brain = tracked
    brain.cfg.hardware.xy_map = "-X-Y"                 # wrong on purpose
    x0, y0 = brain.xy.read_xy()
    n = brain.calibrate_xy_map(3.0)
    assert _wait(lambda: brain.status().xycal_id == n and not brain.status().xycal_running,
                 timeout=30)
    st = brain.status()
    assert st.xycal_state == "OK", st.xycal_state
    res = brain.get_xycal()
    assert res["xy_map"] == "+X+Y" and st.xycal_map == "+X+Y"
    assert res["current"] == "-X-Y" and "DIFFERENT" in res["summary"]
    for k, s in res["scale"].items():                 # the sim moves what it is told
        assert s == pytest.approx(1.0, rel=0.15), (k, s)
    assert brain.cfg.hardware.xy_map == "-X-Y", "nothing is changed by the calibration"
    x1, y1 = brain.xy.read_xy()
    assert (x1, y1) == pytest.approx((x0, y0), abs=1e-6)


def test_calibrate_stage_refuses_without_a_pattern_or_with_the_stabiliser_on(tracked):
    brain = tracked
    brain.set_stabilize(True)
    with pytest.raises(RuntimeError, match="stabiliser"):
        brain.calibrate_xy_map(3.0)
    brain.set_stabilize(False)
    brain.set_tracking(False)
    assert _wait(lambda: not brain.status().tracking_on)
    with pytest.raises(RuntimeError, match="pattern"):
        brain.calibrate_xy_map(3.0)


def test_a_wrong_map_stops_the_stabiliser_instead_of_walking_away(tracked):
    brain = tracked
    brain.cfg.hardware.xy_map = "-X-Y"                 # both axes inverted
    brain.cfg.stabilizer.gain = 0.5
    sc = brain.cfg.scanning
    brain.set_selected_index(sc.points_x - 1, sc.points_y - 1)   # a point off the spot
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().stabilize_on)  # the status follows a frame later
    assert _wait(lambda: not brain.status().stabilize_on, timeout=20), \
        "the stabiliser kept walking the sample away"
    assert any(lvl == "error" and "xy_map" in msg and "Calibrate stage" in msg
               for lvl, msg in brain.events)


def test_the_right_map_does_not_trip_the_guard(tracked):
    brain = tracked
    brain.cfg.hardware.xy_map = "+X+Y"
    sc = brain.cfg.scanning
    brain.set_selected_index(sc.points_x - 1, sc.points_y - 1)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().point_settled, timeout=20)
    assert brain.status().stabilize_on
    assert not any("STOPPED" in msg for _lvl, msg in brain.events)


def test_stop_motion_switches_the_loops_off_and_stops_the_stages(tracked):
    brain = tracked
    calls = []
    brain.xy.stop = lambda: calls.append("xy")
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().stabilize_on)
    res = brain.stop_motion()
    assert "XY stage stopped" in res["stopped"] and calls == ["xy"]
    assert _wait(lambda: not brain.status().stabilize_on)
    assert any(lvl == "warn" and msg.startswith("STOP:") for lvl, msg in brain.events)


def test_a_limit_switch_stops_the_stabiliser_by_default(tracked):
    brain = tracked
    assert brain.cfg.hardware.limit_switch_action == "stop"
    brain.xy.limit_switches = lambda: [True, False]      # X at its limit
    sc = brain.cfg.scanning
    brain.set_selected_index(sc.points_x - 1, sc.points_y - 1)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().stabilize_on)
    assert _wait(lambda: not brain.status().stabilize_on, timeout=20)
    assert any("limit switch" in msg and "limit_switch_action = warn" in msg
               for _lvl, msg in brain.events)


def test_limit_switch_action_warn_carries_on(tracked):
    """Lukas 2026-11-11: the Blue Box Agilis reports X's limit switch active all
    the time while X moves freely -> that rig sets warn: it converges and says so."""
    brain = tracked
    brain.cfg.hardware.limit_switch_action = "warn"
    brain.xy.limit_switches = lambda: [True, False]
    sc = brain.cfg.scanning
    brain.set_selected_index(sc.points_x - 1, sc.points_y - 1)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().point_settled, timeout=20)
    assert brain.status().stabilize_on
    assert not any("STOPPED" in msg for _lvl, msg in brain.events)
    assert any(lvl == "warn" and "continuing (limit_switch_action = warn)" in msg
               for lvl, msg in brain.events)


def test_limit_switch_action_is_sanitised_and_round_trips(tmp_path):
    from camera.config import load_config, save_config
    cfg = Config()
    cfg.hardware.limit_switch_action = "warn"
    save_config(cfg, str(tmp_path / "c.ini"))
    assert load_config(str(tmp_path / "c.ini")).hardware.limit_switch_action == "warn"
    cfg.hardware.limit_switch_action = "ignore"
    save_config(cfg, str(tmp_path / "c.ini"))
    assert load_config(str(tmp_path / "c.ini")).hardware.limit_switch_action == "stop"


def test_a_stage_that_does_not_move_the_image_stops_the_stabiliser(tracked):
    brain = tracked
    brain.xy.move_xy = lambda x, y: None                 # counts, never moves
    sc = brain.cfg.scanning
    brain.set_selected_index(sc.points_x - 1, sc.points_y - 1)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().stabilize_on)
    assert _wait(lambda: not brain.status().stabilize_on, timeout=20)
    assert any("does not" in msg and "move" in msg for _lvl, msg in brain.events)


def test_calibrate_stage_proposes_and_writes_the_real_step_sizes(tracked):
    """Lukas 2026-11-11 ("Camera writes it"): the stage really moves 2x what it
    is told (an uncalibrated Agilis); Calibrate stage proposes um/step x 2 per
    direction and writes them into the stage service."""
    brain = tracked
    brain.cfg.stabilizer.xycal_repeats = 2
    real_move = brain.xy.move_xy
    x0, y0 = brain.xy.read_xy()
    brain.xy.move_xy = lambda x, y: real_move(x0 + 2 * (x - x0), y0 + 2 * (y - y0))
    brain.xy.step_sizes = lambda: {"+X": 0.05, "-X": 0.05, "+Y": 0.05, "-Y": 0.04}
    written = {}
    brain.xy.write_step_sizes = lambda sizes: (written.update(sizes)
                                               or {"written": dict(sizes),
                                                   "saved": "agilis.ini", "note": ""})
    with pytest.raises(RuntimeError, match="run Calibrate stage first"):
        brain.write_xy_step_sizes()
    n = brain.calibrate_xy_map(2.0)
    assert _wait(lambda: brain.status().xycal_id == n and not brain.status().xycal_running,
                 timeout=60)
    assert brain.status().xycal_state == "OK", brain.status().xycal_state
    res = brain.get_xycal()
    assert res["repeats"] == 2 and len(res["moves"]) == 8
    for k, want in (("+X", 0.10), ("-X", 0.10), ("+Y", 0.10), ("-Y", 0.08)):
        assert res["steps_new"][k] == pytest.approx(want, rel=0.15), (k, res["steps_new"])
    out = brain.write_xy_step_sizes()
    assert written == res["steps_new"] and out["saved"] == "agilis.ini"


def test_the_stage_client_writes_and_saves_step_sizes():
    from camera.backends.remote_kim import StepXYStage

    class Link:
        def __init__(self, can_save=True):
            self.calls, self.can_save = [], can_save
        def status(self):
            return {"um_per_step": [0.05, 0.05], "um_per_step_fwd": [0.05, 0.06],
                    "um_per_step_bwd": [0.0, 0.04]}
        def rpc(self, **req):
            self.calls.append(req)
            if req["cmd"] == "save_calibration":
                if not self.can_save:
                    raise RuntimeError("unknown command 'save_calibration'")
                return {"ok": True, "path": "C:/x/agilis.ini"}
            return {"ok": True}

    link = Link()
    st = StepXYStage(link)
    assert st.step_sizes() == {"+X": 0.05, "-X": 0.05, "+Y": 0.06, "-Y": 0.04}
    out = st.write_step_sizes({"+X": 0.2, "-Y": 0.1})
    assert {"cmd": "set_calibration", "axis": "X", "value": 0.2, "direction": 1} in link.calls
    assert {"cmd": "set_calibration", "axis": "Y", "value": 0.1, "direction": -1} in link.calls
    assert link.calls[-1] == {"cmd": "save_calibration"} and out["saved"] == "C:/x/agilis.ini"
    old = StepXYStage(Link(can_save=False))
    out = old.write_step_sizes({"+X": 0.2})
    assert out["saved"] == "" and "NOT saved" in out["note"]


def test_wait_arrived_waits_for_the_step_counters_not_a_stale_moving_flag():
    """Blue Box 2026-11-11, paced Agilis (10 steps/s): the status said "not
    moving" right after the command (a stale frame, and a paced move between
    chunks), so Calibrate stage measured 4 of 60 steps. wait_arrived waits for
    the counters to reach the targets the service replied."""
    import threading
    from camera.backends.remote_kim import StepXYStage

    class PacedAgilis:
        def __init__(self):
            self.pos, self.target = [0, 0], [0, 0]
        def status(self):
            # NEVER says moving (the worst case of a stale / chunked frame)
            return {"position_steps": list(self.pos), "moving": [False, False],
                    "position_um": [p * 0.05 for p in self.pos]}
        def rpc(self, **req):
            if req["cmd"] == "move_to_um":
                a = 0 if req["axis"] == "X" else 1
                self.target[a] = int(round(req["position"] / 0.05))
                return {"ok": True, "target": self.target[a]}
            return {"ok": True}

    link = PacedAgilis()

    def walk():                      # 10 steps per 0.05 s, toward the target
        for _ in range(200):
            for a in (0, 1):
                d = link.target[a] - link.pos[a]
                link.pos[a] += max(-10, min(10, d))
            time.sleep(0.01)

    st = StepXYStage(link)
    st.move_xy(3.0, 0.0)                               # 60 steps on X
    threading.Thread(target=walk, daemon=True).start()
    t0 = time.monotonic()
    st.wait_arrived(timeout_s=5)
    assert link.pos == [60, 0], "returned before the stage got there"
    assert time.monotonic() - t0 > 0.03
