"""Per-objective AUTOFOCUS distances, and kim saving the Z step sizes (2026-09-29).

Lukas: "suggest zcal_max_travel 8 um, but this differs between objective
lenses". The focal depth scales as ~1/NA^2, so the autofocus / Z step
calibration distances belong to the objective. An objectives.ini section may
name any of objectives.AF_KEYS; absent keys keep the autofocus config value.
Applied when the objective is set (and at start); the AutoFocus tab tags the
values that come from the objective and offers to store an edit for it.

And: after writing the Z step sizes into kim, the calibration asks kim to SAVE
them (kim's new ``save_calibration``); an older kim without it -> "live only".
"""

import os
import time

import pytest

from camera import objectives as OBJ
from camera.backends.sim import SimCamera, SimSlipStickZ, SimXYStage
from camera.camera import Camera
from camera.config import Config, load_config

INI = """# my microscope (this comment must survive a store)
[20x]
pixel_size_x_um = 0.4130
pixel_size_y_um = 0.4130

[63x]
pixel_size_x_um = 0.04562
pixel_size_y_um = 0.04562
zcal_max_travel_v = 8
max_travel_v = 12
coarse_step_v = 2
fine_step_v = 0.25
"""


def _brain(tmp_path, objective="20x"):
    p = tmp_path / "objectives.ini"
    p.write_text(INI, encoding="utf-8")
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    cfg.image.objectives_file = str(p)
    cfg.image.objective_name = objective
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = SimSlipStickZ(z0=30.0, z_focus=30.0, vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v)
    cam = SimCamera(xy, z, pixel_size_x_um=0.4, pixel_size_y_um=0.4)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    return brain, p, events


def test_the_table_reads_the_autofocus_keys_and_ignores_bad_ones(tmp_path):
    p = tmp_path / "o.ini"
    p.write_text(INI + "[5x]\npixel_size_x_um = 1.6\ncoarse_step_v = lots\n", encoding="utf-8")
    t = OBJ.load_objectives(str(p))
    assert t["63x"].af == {"zcal_max_travel_v": 8.0, "max_travel_v": 12.0,
                           "coarse_step_v": 2.0, "fine_step_v": 0.25}
    assert t["20x"].af == {}
    assert t["5x"].af == {}                        # not a number: ignored, never guessed
    # round trip through save_objectives
    q = tmp_path / "copy.ini"
    OBJ.save_objectives(t, str(q))
    assert OBJ.load_objectives(str(q))["63x"].af == t["63x"].af


def test_store_changes_one_line_and_keeps_the_comments(tmp_path):
    p = tmp_path / "o.ini"
    p.write_text(INI, encoding="utf-8")
    OBJ.store_af_value(str(p), "63x", "coarse_step_v", 1.5)       # replace
    OBJ.store_af_value(str(p), "20x", "zcal_max_travel_v", 30)    # add to a section
    OBJ.store_af_value(str(p), "63x", "fine_step_v", None)        # remove
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# my microscope (this comment must survive a store)")
    t = OBJ.load_objectives(str(p))
    assert t["63x"].af == {"zcal_max_travel_v": 8.0, "max_travel_v": 12.0,
                           "coarse_step_v": 1.5}
    assert t["20x"].af == {"zcal_max_travel_v": 30.0}
    assert t["20x"].pixel_size_x_um == pytest.approx(0.413)
    with pytest.raises(ValueError):
        OBJ.store_af_value(str(p), "63x", "park_tolerance", 0.1)   # not a distance


def test_the_objective_sets_its_distances_and_switching_puts_back_the_rest(tmp_path):
    brain, p, events = _brain(tmp_path, "20x")
    af = brain.cfg.autofocus
    base = {k: getattr(af, k) for k in OBJ.AF_KEYS}
    assert brain._af_objective_keys() == ""                      # 20x names none
    brain.set_objective("63x")
    assert (af.zcal_max_travel_v, af.max_travel_v, af.coarse_step_v, af.fine_step_v) == \
        (8.0, 12.0, 2.0, 0.25)
    assert af.drive_amplitude_v == base["drive_amplitude_v"]     # absent -> config value
    assert set(brain._af_objective_keys().split(",")) == {
        "zcal_max_travel_v", "max_travel_v", "coarse_step_v", "fine_step_v"}
    assert any("autofocus from the objective" in m for _l, m in events)
    # a session edit (set_config) is NOT undone by the next apply_config ...
    brain.set_config({"autofocus": {"coarse_step_v": 3.0}})
    assert af.coarse_step_v == 3.0
    assert "coarse_step_v" not in brain._af_objective_keys()     # no longer "from 63x"
    brain.set_config({"spot": {"thr_lower": 120}})               # unrelated Apply
    assert af.coarse_step_v == 3.0
    # ... and switching back restores every key 63x had replaced
    brain.set_objective("20x")
    assert {k: getattr(af, k) for k in OBJ.AF_KEYS} == base
    assert brain._af_objective_keys() == ""


def test_at_start_the_current_objective_applies_and_status_says_which(tmp_path):
    brain, p, events = _brain(tmp_path, "63x")
    try:
        assert brain.cfg.autofocus.zcal_max_travel_v == 8.0
        brain.start()
        t0 = time.monotonic()
        while brain.status().frame_number < 3 and time.monotonic() - t0 < 10:
            time.sleep(0.02)
        keys = set(brain.status().af_objective_keys.split(","))
        assert keys == {"zcal_max_travel_v", "max_travel_v", "coarse_step_v", "fine_step_v"}
    finally:
        brain.shutdown()


def test_save_config_keeps_camera_ini_objective_neutral(tmp_path):
    brain, p, events = _brain(tmp_path, "20x")
    base = brain.cfg.autofocus.max_travel_v
    brain.set_objective("63x")
    ini = tmp_path / "camera.ini"
    brain.save_config(str(ini))
    assert load_config(str(ini)).autofocus.max_travel_v == base     # not the 63x 12
    assert brain.cfg.autofocus.max_travel_v == 12.0                 # still in use
    brain.set_config({"autofocus": {"coarse_step_v": 3.5}})         # the user's own value
    brain.save_config(str(ini))
    assert load_config(str(ini)).autofocus.coarse_step_v == 3.5


def test_store_for_the_objective_writes_the_file_and_the_base_stays(tmp_path):
    brain, p, events = _brain(tmp_path, "63x")
    af = brain.cfg.autofocus
    base_amp = af.drive_amplitude_v
    brain.set_config({"autofocus": {"drive_amplitude_v": 5.0}})     # edited, then stored
    rep = brain.store_objective_af("drive_amplitude_v", 5.0, previous=base_amp)
    assert rep["objective"] == "63x" and rep["value"] == 5.0
    assert OBJ.load_objectives(str(p))["63x"].af["drive_amplitude_v"] == 5.0
    assert "drive_amplitude_v" in brain._af_objective_keys()
    assert "comment must survive" in p.read_text(encoding="utf-8")
    brain.set_objective("20x")
    assert af.drive_amplitude_v == base_amp                         # the pre-edit value
    brain.set_objective("63x")
    assert af.drive_amplitude_v == 5.0


def test_store_over_the_wire(tmp_path):
    from camera.net.client import CameraClient
    from camera.net.service import CameraService
    brain, p, events = _brain(tmp_path, "63x")
    svc = CameraService(brain, cmd_port=17491, pub_port=17492)
    svc.start()
    try:
        cli = CameraClient("localhost", cmd_port=17491, pub_port=17492)
        rep = cli.store_objective_af("coarse_step_v", 1.25)
        assert rep["value"] == 1.25 and rep["objective"] == "63x"
        assert OBJ.load_objectives(str(p))["63x"].af["coarse_step_v"] == 1.25
        cli.close()
    finally:
        svc.stop()
        brain.shutdown()


def test_the_autofocus_tab_tags_and_offers_to_store(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from camera.apps.gui import MainWindow
    app = QApplication.instance() or QApplication([])
    brain, p, events = _brain(tmp_path, "63x")
    try:
        win = MainWindow(brain, brain.cfg)
        win._refresh_objective_af(_st(brain))
        lab = win._form_labels["autofocus"]["coarse_step_v"]
        assert "[63x]" in lab.text().replace("\u200b", "")
        assert "[63x]" not in win._form_labels["autofocus"]["drive_amplitude_v"].text()
        # edit + Apply -> asked; "No" = this session only, file unchanged
        asked = []
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1])
        win._form_widgets["autofocus"]["fine_step_v"].setValue(0.5)
        win._apply_settings([("Autofocus", brain.cfg.autofocus)])
        assert asked and "63x" in asked[-1] and "fine_step_v" in asked[-1]
        assert brain.cfg.autofocus.fine_step_v == 0.5
        assert OBJ.load_objectives(str(p))["63x"].af["fine_step_v"] == 0.25
        # "Yes" = stored in objectives.ini for 63x
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        win._form_widgets["autofocus"]["fine_step_v"].setValue(0.4)
        win._apply_settings([("Autofocus", brain.cfg.autofocus)])
        assert OBJ.load_objectives(str(p))["63x"].af["fine_step_v"] == 0.4
        # a non-distance edit never asks
        asked.clear()
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1])
        win._form_widgets["autofocus"]["steps"].setValue(17)
        win._apply_settings([("Autofocus", brain.cfg.autofocus)])
        assert not asked
        win.close()
        app.processEvents()
    finally:
        brain.shutdown()


def _st(brain):
    st = brain.status()
    st.af_objective_keys = brain._af_objective_keys()
    st.objective_name = brain.cfg.image.objective_name
    return st


# --------------------------------------------------------------------------- #
# kim saves the Z step sizes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("has_verb", [True, False])
def test_kim_z_focus_asks_kim_to_save(has_verb):
    import test_remote_kim as RK
    from camera.backends.remote_kim import KimLink, KimZFocus
    kim = RK.FakeKimService(has_save=has_verb)
    link = KimLink("127.0.0.1", kim.cmd_port, kim.pub_port)
    try:
        link.open()
        z = KimZFocus(link)
        out = z.save_step_sizes()
        if has_verb:
            assert out == "C:/kim/kim.ini" and kim.saves == 1
        else:
            assert out is None                  # "unknown save_calibration" -> live only
    finally:
        link.close()
        kim.close()


class _SavingZ(SimSlipStickZ):
    """A slip-stick Z that can save its step sizes like kim (or not)."""
    mode = "ok"
    saved = 0

    def save_step_sizes(self):
        if self.mode == "old":
            return None
        if self.mode == "fail":
            raise RuntimeError("kim save_calibration: disk full")
        _SavingZ.saved += 1
        return "C:/kim/kim.ini"


@pytest.mark.parametrize("mode", ["ok", "old", "fail"])
def test_the_calibration_saves_the_step_sizes_in_kim(mode, monkeypatch):
    import test_z_step_calibration as T
    monkeypatch.setattr(T, "SimSlipStickZ", _SavingZ)
    _SavingZ.mode, _SavingZ.saved = mode, 0
    brain, z, events = T._rig()
    try:
        s = T._run_zcal(brain)
        assert s.zcal_state == "OK", s.zcal_state
        msg = [m for _l, m in events if "ratio up/down" in m][-1]
        if mode == "ok":
            assert _SavingZ.saved == 1 and "saved by kim to C:/kim/kim.ini" in msg
        elif mode == "old":
            assert "live only" in msg
            assert any(lvl == "warn" and "no save_calibration" in m for lvl, m in events)
        else:
            assert "could NOT save" in msg and "disk full" in msg
    finally:
        brain.shutdown()
