"""Autofocus as a scan routine (2026-09-24).

Lukáš: call autofocus from the measurement suite and WAIT for it to finish;
while it runs, do not stabilise and do not track -- the image may wander off.

- every request gets a number (`af_id`); a waiter knows its run is over when
  status shows that number and `af_running` False;
- a second request queued during a run keeps `af_running` True until IT is done;
- no XY correction is sent while a run is in progress, `point_settled` drops,
  and the stabiliser earns it again afterwards on its own.
"""

import threading
import time

import pytest

from camera.config import Config
from camera.net.describe import build_manifest
from camera.sim_system import build_sim_system


def _wait(cond, timeout=10.0, poll=0.01):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


@pytest.fixture
def system():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.autofocus.drive_amplitude_v = 12.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(10)
    yield brain, cam, xy, z
    brain.shutdown()


def test_each_request_gets_a_number_and_ends_ok(system):
    brain, *_ = system
    n1 = brain.autofocus()
    s = brain.status()
    assert s.af_id == n1 and s.af_running            # visible at once, not a frame later
    assert _wait(lambda: not brain.status().af_running)
    s = brain.status()
    assert (s.af_id, s.af_error) == (n1, "OK")
    n2 = brain.autofocus()
    assert n2 == n1 + 1
    assert _wait(lambda: brain.status().af_id == n2 and not brain.status().af_running)


def test_a_request_queued_during_a_run_keeps_it_busy_until_it_ran(system):
    """The waiter for #2 must not return when #1 finishes."""
    brain, *_ = system
    done = []
    orig = brain._run_autofocus

    def counting(req):
        orig(req)
        done.append(req["id"])
    brain._run_autofocus = counting

    n1 = brain.autofocus()
    assert _wait(lambda: brain.status().af_error == "running")
    n2 = brain.autofocus()
    seen_early = False
    t_end = time.monotonic() + 20
    while time.monotonic() < t_end:
        s = brain.status()
        if s.af_id == n2 and not s.af_running:
            seen_early = n2 not in done
            break
        time.sleep(0.005)
    assert not seen_early
    assert done == [n1, n2]


def test_killing_a_queued_request_ends_it(system):
    brain, *_ = system
    brain.cfg.autofocus.steps = 60                   # a long first run
    n1 = brain.autofocus()
    assert _wait(lambda: brain.status().af_error == "running")
    brain.autofocus()
    brain.kill_af()                                  # stops #1 AND cancels #2
    assert _wait(lambda: not brain.status().af_running)
    assert brain.status().af_error == "killed"


def test_no_stabiliser_move_during_autofocus_and_it_resumes_after(system):
    brain, cam, xy, z = system
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    brain.set_tracking(True)
    brain.set_selected_index(0, 0)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().point_settled, timeout=15)

    moves = []                                       # (during_af?) per XY command
    orig_move = xy.move_xy

    def spy(*a, **kw):
        moves.append(brain._af_state in ("running", "queued"))
        return orig_move(*a, **kw)
    xy.move_xy = spy

    brain.cfg.autofocus.steps = 30
    brain.set_selected_index(2, 2)                   # the stabiliser now WANTS to move...
    brain.autofocus()                                # ...but autofocus comes first
    settled_during = []
    while brain.status().af_running:
        settled_during.append(brain.status().point_settled)
        time.sleep(0.01)
    assert brain.status().af_error == "OK"
    assert not any(settled_during)                   # never "settled" while Z moves
    # a correction may have gone out in the instant before the request (False);
    # none while it was queued or running (True)
    assert not any(moves)
    # tracking + stabiliser were paused, not switched off: they carry on
    s = brain.status()
    assert s.tracking_on and s.stabilize_on
    assert _wait(lambda: brain.status().point_settled, timeout=15)


def test_describe_offers_autofocus_as_a_waitable_scan_action(system):
    brain, *_ = system
    d = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    w = d["autofocus"]["wait"]
    assert w["target_key"] == "af_id"
    assert w["ready"] == {"policy": "adopt_then_flag", "setpoint_key": "af_id",
                          "flag_key": "af_running", "invert": True}
    assert w["check"] == {"key": "af_error", "equals": "OK"}
    assert w["timeout_s"] == brain.cfg.autofocus.scan_timeout_s
    assert d["af_id"]["read_path"] == ["af_id"]
