"""A lost pattern is a FAULT, not something to paper over (2026-09-28).

Lukas's decision (his words): "A whole-frame relock is dangerous for spurious
templates. The template never changes abruptly, so if it is lost it is out of
focus, out of image, the spot is in the pattern, or something else happened
that is terrible. All need correction. We can give a new possibility that when
the pattern is lost and not close to the edge or near the spot, we allow an
autofocus. It should definitely stop the measurement suite -- or pause for user
correction."

So these tests check that:
  * losing the pattern for `lost_frames` frames sets a LATCHED `fault` that says
    the likely cause (out of image / spot on the pattern / out of focus);
  * while faulted the stabiliser holds the stage and `point_settled` is False,
    even after the pattern is found again -- only `clear_fault` ends it, and
    clearing is refused while the pattern is still not found;
  * there is NO whole-frame relock: a pattern that jumped away is not re-found
    somewhere else in the frame;
  * the optional `autofocus_on_loss` runs ONE autofocus when the cause is
    neither the edge nor the spot, and clears the fault only if the pattern is
    then found again at its last place;
  * a failed frame grab shows up as `hw_error` and never leaves a stale
    `point_settled` behind.

The engine is driven by hand (`_tick` = one iteration of Camera._run) so every
test is deterministic and fast: no thread, no timing.
"""

from __future__ import annotations

import pytest

from camera.config import Config, load_config, save_config
from camera.net import protocol as P
from camera.net.describe import build_manifest
from camera.net.service import CameraService
from camera.sim_system import build_sim_system

PX = 0.413   # um per px of the sim scene (Config default)


def _tick(brain) -> None:
    """One iteration of Camera._run, without the thread: a queued autofocus
    runs (as the engine would), otherwise one frame is processed."""
    with brain._lock:
        req, brain._af_request = brain._af_request, None
        if req is not None:
            brain._af_kill.clear()
    if req is not None:
        brain._do_autofocus(req)
    else:
        brain._process()


def _system(**pattern):
    cfg = Config()
    cfg.stabilizer.settle_s = 0.0          # frames come by hand: no wall-clock waits
    cfg.stabilizer.images_to_average = 1
    cfg.autofocus.drive_amplitude_v = 50.0  # a sweep wide enough to find a big defocus
    cfg.autofocus.steps = 21
    cfg.autofocus.averages_per_level = 1
    cfg.hardware.z_step_time_ms = 0.0
    for k, v in pattern.items():
        setattr(cfg.pattern, k, v)
    brain, cam, xy, z = build_sim_system(cfg)
    # the spot position is a user calibration; the sim spot sits at (320, 240)
    cfg.spot.ref_set, cfg.spot.ref_x, cfg.spot.ref_y = True, 320.0, 240.0
    events: list = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    _tick(brain)
    tx, ty = cam.template_center_px()
    brain.capture_reference((tx, ty, 60, 60))
    brain.set_tracking(True)
    _tick(brain)
    assert brain.status().match_found
    return brain, cam, xy, z, events


def _ticks(brain, n):
    for _ in range(n):
        _tick(brain)
    return brain.status()


# --------------------------------------------------------------------------- #
# the latched fault and its causes
# --------------------------------------------------------------------------- #
def test_defocus_latches_a_fault_that_names_focus():
    brain, cam, xy, z, _ = _system()
    assert brain.status().fault == ""
    z.set_z(30.0)                              # 22 V out of focus: the match fails
    s = _ticks(brain, 1)
    assert not s.match_found
    assert s.fault == "", "one missed frame is not yet a loss"
    s = _ticks(brain, brain.cfg.pattern.lost_frames)
    assert "lost" in s.fault and "focus" in s.fault, s.fault
    assert not s.point_settled


def test_pattern_leaving_the_image_says_out_of_image():
    brain, cam, xy, z, _ = _system(autofocus_on_loss=True)
    af0 = brain.status().af_id
    x0, y0 = xy.read_xy()
    # walk the sample right in 20 px steps; tracking follows until it leaves
    for k in range(1, 20):
        xy.move_xy(x0 + k * 20 * PX, y0)
        _tick(brain)
    s = _ticks(brain, brain.cfg.pattern.lost_frames)
    assert "out of image" in s.fault, s.fault
    # near the edge an autofocus cannot help: none was started
    assert s.af_id == af0 and not s.af_running


def test_spot_on_the_pattern_says_so():
    brain, cam, xy, z, _ = _system(autofocus_on_loss=True)
    cam.base_sigma = 12.0                     # a big bright spot that spoils the match
    af0 = brain.status().af_id
    x0, y0 = xy.read_xy()
    for k in range(0, 10):                    # template 440 -> 350 px, onto the spot
        xy.move_xy(x0 - k * 10 * PX, y0 - k * 1.7 * PX)
        _tick(brain)
    s = _ticks(brain, brain.cfg.pattern.lost_frames + 2)
    assert "spot" in s.fault, s.fault
    assert s.af_id == af0, "no autofocus when the spot sits on the pattern"


def test_no_whole_frame_relock():
    """The pattern jumps 150 px (more than the safety box) and stays fully in
    view: it must NOT be re-found elsewhere in the frame."""
    brain, cam, xy, z, _ = _system()
    x0, y0 = xy.read_xy()
    xy.move_xy(x0 - 150 * PX, y0 + 120 * PX)   # template 440,260 -> 290,380: in view
    s = _ticks(brain, brain.cfg.pattern.lost_frames + 20)
    assert not s.match_found
    assert s.fault


# --------------------------------------------------------------------------- #
# latched: the stabiliser holds, point_settled stays False, clear_fault ends it
# --------------------------------------------------------------------------- #
def test_fault_holds_the_stage_until_cleared():
    brain, cam, xy, z, _ = _system()
    brain.set_selected_index(2, 2)            # a point ~1.4 um off the spot
    brain.set_stabilize(True)
    s = _ticks(brain, 40)
    assert s.point_settled
    z.set_z(30.0)
    s = _ticks(brain, brain.cfg.pattern.lost_frames + 1)
    assert s.fault
    with pytest.raises(RuntimeError, match="not found"):
        brain.clear_fault()                   # still lost: refused
    z.set_z(7.6)                              # the user refocuses by hand
    brain.set_selected_index(0, 0)            # ... and asks for another point
    held = xy.read_xy()
    s = _ticks(brain, 20)
    assert s.match_found, "found again at its last place (local search)"
    assert s.fault, "the fault is LATCHED: finding the pattern does not clear it"
    assert xy.read_xy() == pytest.approx(held), "the stabiliser moved while faulted"
    assert not s.point_settled and not s.stable
    brain.clear_fault()
    assert brain.status().fault == ""
    s = _ticks(brain, 40)
    assert s.fault == ""
    assert xy.read_xy() != pytest.approx(held), "the stabiliser resumes after clearing"
    assert s.point_settled


def test_clear_fault_without_a_fault_is_harmless():
    brain, *_ = _system()
    brain.clear_fault()
    assert brain.status().fault == ""


# --------------------------------------------------------------------------- #
# autofocus_on_loss
# --------------------------------------------------------------------------- #
def test_autofocus_on_loss_recovers_a_defocused_pattern():
    brain, cam, xy, z, events = _system(autofocus_on_loss=True)
    af0 = brain.status().af_id
    z.set_z(30.0)
    n = brain.cfg.pattern.lost_frames
    s = _ticks(brain, n)                      # the loss is declared; the AF is queued
    assert "autofocus" in s.fault and "running" in s.fault, s.fault
    assert s.af_id == af0 + 1 and s.af_running
    assert not s.point_settled
    _tick(brain)                              # the engine runs the autofocus
    assert abs(z.read_z() - 7.6) < 1.5
    s = _ticks(brain, n + 2)
    assert s.match_found
    assert s.fault == "", s.fault
    assert any(lvl == "warn" and "recovered by autofocus" in msg for lvl, msg in events)


def test_autofocus_on_loss_fails_to_a_latched_fault():
    """Lost for a reason focus cannot fix (the sample jumped): ONE autofocus,
    then the fault latches -- and no second autofocus is started."""
    brain, cam, xy, z, _ = _system(autofocus_on_loss=True)
    af0 = brain.status().af_id
    x0, y0 = xy.read_xy()
    xy.move_xy(x0 - 150 * PX, y0 + 120 * PX)
    s = _ticks(brain, brain.cfg.pattern.lost_frames + 1)   # declared + AF run
    assert s.af_id == af0 + 1
    s = _ticks(brain, brain.cfg.pattern.lost_frames + 20)
    assert s.fault and "autofocus" in s.fault and "running" not in s.fault, s.fault
    assert s.af_id == af0 + 1, "only ONE autofocus per loss"
    assert not s.match_found


def test_autofocus_on_loss_is_off_by_default():
    cfg = Config()
    assert cfg.pattern.autofocus_on_loss is False
    brain, cam, xy, z, _ = _system()
    af0 = brain.status().af_id
    z.set_z(30.0)
    s = _ticks(brain, brain.cfg.pattern.lost_frames + 3)
    assert s.fault and s.af_id == af0


# --------------------------------------------------------------------------- #
# hw_error
# --------------------------------------------------------------------------- #
def test_grab_failure_is_hw_error_and_never_settled():
    brain, cam, xy, z, _ = _system()
    brain.set_selected_index(1, 1)
    brain.set_stabilize(True)
    s = _ticks(brain, 40)
    assert s.point_settled and s.hw_error == ""
    real = cam.grab

    def broken():
        raise OSError("USB gone")

    cam.grab = broken
    _tick(brain)
    s = brain.status()
    assert "USB gone" in s.hw_error
    assert not s.point_settled and not s.stable and not s.laser_settled
    cam.grab = real
    s = _ticks(brain, 1)
    assert s.hw_error == ""


# --------------------------------------------------------------------------- #
# config + wire
# --------------------------------------------------------------------------- #
def test_new_pattern_keys_travel_ini_and_wire(tmp_path):
    cfg = Config()
    cfg.pattern.autofocus_on_loss = True
    cfg.pattern.lost_frames = 7
    cfg.pattern.loss_edge_margin_px = 33
    cfg.pattern.loss_spot_margin_px = 44
    path = str(tmp_path / "c.ini")
    save_config(cfg, path)
    back = load_config(path)
    assert back.pattern.autofocus_on_loss is True
    assert (back.pattern.lost_frames, back.pattern.loss_edge_margin_px,
            back.pattern.loss_spot_margin_px) == (7, 33, 44)
    d = P.config_to_dict(cfg)["pattern"]
    assert d["autofocus_on_loss"] is True
    other = Config()
    P.apply_config_dict(other, {"pattern": {"autofocus_on_loss": True}})
    assert other.pattern.autofocus_on_loss is True


def test_describe_and_service_know_fault():
    brain, cam, xy, z, _ = _system()
    ids = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    assert ids["clear_fault"]["kind"] == "action" and ids["clear_fault"].get("help")
    assert ids["fault"]["read_path"] == ["fault"]
    assert ids["hw_error"]["read_path"] == ["hw_error"]
    svc = CameraService(brain, cmd_port=17563, pub_port=17564)   # never started
    assert svc._dispatch({"cmd": "clear_fault"})["ok"]
    st = svc._dispatch({"cmd": "status"})["status"]
    assert st["fault"] == "" and st["hw_error"] == ""
