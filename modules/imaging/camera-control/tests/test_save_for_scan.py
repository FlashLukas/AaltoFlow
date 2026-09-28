"""Saving the pattern and a camera picture NEXT TO a measurement (scan routines).

scan-core fills {data_dir} / {data_stem} / {moment} in the action arguments, so
the camera writes into the measurement's folder. Here: the files and their
.json records, no overwriting, and the describe entries that make scan-core
offer both as routine actions that do not wait for anything else.
"""
import json
import time
from pathlib import Path

import pytest

from camera.config import Config
from camera.net.describe import build_manifest
from camera.sim_system import build_sim_system


@pytest.fixture
def brain():
    cfg = Config()
    b, *_ = build_sim_system(cfg)
    b.start()
    t0 = time.monotonic()
    while b.latest_frame() is None and time.monotonic() - t0 < 5:
        time.sleep(0.05)
    yield b
    b.shutdown()


def test_the_pattern_is_saved_with_its_scan_array_and_a_record(brain, tmp_path):
    brain.cfg.scanning.points_x, brain.cfg.scanning.points_y = 7, 5
    brain.capture_reference((200, 240, 60, 60))
    res = brain.save_scan_pattern(str(tmp_path), "134501_map_before_pattern")
    png, info = Path(res["path"]), Path(res["info"])
    assert png.name == "134501_map_before_pattern.png" and png.exists()
    rec = json.loads(info.read_text(encoding="utf-8"))
    assert rec["kind"] == "pattern"
    assert rec["scanning"]["scanning"]["points_x"] == 7
    assert rec["scanning"]["points_y"] == 5
    # loading it back restores the array
    brain.cfg.scanning.points_x = 2
    brain.load_pattern(str(png))
    assert brain.cfg.scanning.points_x == 7
    # a second save with the same name does not overwrite the first
    again = brain.save_scan_pattern(str(tmp_path), "134501_map_before_pattern")
    assert Path(again["path"]).name == "134501_map_before_pattern_2.png"


def test_no_pattern_is_an_error_not_an_empty_file(brain, tmp_path):
    brain.reference = None
    with pytest.raises(RuntimeError, match="no pattern"):
        brain.save_scan_pattern(str(tmp_path), "x")
    assert not list(tmp_path.iterdir())


def test_a_picture_is_the_full_frame_plus_a_record(brain, tmp_path):
    import cv2
    res = brain.save_picture(str(tmp_path), "run_after_camera")
    img = cv2.imread(res["path"], cv2.IMREAD_UNCHANGED)
    assert img.shape[:2] == brain.latest_frame().shape[:2]
    rec = json.loads(Path(res["info"]).read_text(encoding="utf-8"))
    assert rec["kind"] == "picture" and "frame_number" in rec["status"]


def test_outside_a_scan_it_falls_back_to_the_camera_folder(brain, tmp_path):
    brain.cfg.image.save_path = str(tmp_path / "captures")
    res = brain.save_picture("", "")              # placeholders filled with ""
    assert Path(res["path"]).parent == tmp_path / "captures"
    assert Path(res["path"]).name.startswith("camera_")


def test_describe_offers_both_as_routine_actions_with_placeholders(brain):
    m = build_manifest(brain)
    items = {d["id"]: d for d in (m.get("parameters") or m.get("items") or [])}
    for aid, suffix in (("save_scan_pattern", "_pattern"), ("save_picture", "_camera")):
        d = items[aid]
        assert d["wait"]["ready"]["policy"] == "immediate"
        defaults = {a["name"]: a["default"] for a in d["args"]}
        assert defaults["folder"] == "{data_dir}"
        assert defaults["name"] == "{data_stem}_{moment}" + suffix
