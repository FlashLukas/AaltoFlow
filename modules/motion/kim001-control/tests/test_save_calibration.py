"""kim001.ini: step sizes that survive a restart (Lukas, 2026-09-29).

The camera's "Calibrate Z steps" measures the Z up/down step sizes (they differ
by ~20-25 % on the KIM101's PIA25) and writes them with set_calibration. kim-control
first ran WITHOUT any .ini, so a restart forgot them. Here, from the start:
  * the service (and a local GUI) loads kim001.ini from the project folder at
    start when it exists and no --config is given -- and loading a file writes
    NOTHING to the controller (adopt-on-start rule);
  * `save_calibration` persists the Calibration group into that file, merging
    (other sections survive), atomically; `save_config` saves everything;
  * the STEP SIZE card has a Save button, Settings a "Save config" button.

conftest.py points kim001.config.default_config_path at a temp folder, so these
tests never read or write the lab's real kim001.ini. Wire tests use ports
19454/19455 (not the service's 5637/5638, nor the other test files' ports).
"""

from __future__ import annotations

import configparser
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kim001 import config as kcfg
from kim001.backends.sim import SimKim
from kim001.config import Config, load_config, save_config
from kim001.kim001 import Kim
from kim001.sim_system import build_sim_system

CMD, PUB = 19454, 19455
PROJECT = Path(__file__).resolve().parents[1]

# A controller state the .ini would never produce (kim-control's lab KIM101),
# so a brain that pushed the file's drive values would show it.
FOUND = {"position": 183, "rate": 500, "accel": 1000, "voltage": 112}


def _ini_with_z_pair(path: Path) -> Path:
    """A kim001.ini as the lab would have it after "Calibrate Z steps" + Save,
    with drive values that DIFFER from the controller's (125 V / 300 / 5000)."""
    cfg = Config()
    cfg.calibration.um_per_step_z = 0.0161        # up (forward) is the smaller step
    cfg.calibration.um_per_step_z_bwd = 0.0203
    cfg.limits.leash_enabled = True               # a bool that must stay True (gotcha #3)
    cfg.limits.leash_z = 1234
    cfg.motion.voltage_x = cfg.motion.voltage_y = cfg.motion.voltage_z = 125.0
    save_config(cfg, str(path))
    return path


# --------------------------------------------------------------------------- #
# 1. start with a kim001.ini present
# --------------------------------------------------------------------------- #
def test_startup_loads_kim_ini_when_present_and_no_config_given(tmp_path):
    ini = _ini_with_z_pair(kcfg.default_config_path())
    cfg, path, loaded = kcfg.load_startup_config(None)
    assert loaded and path == ini
    assert cfg.calibration.um_per_step_z == pytest.approx(0.0161)
    assert cfg.calibration.um_per_step_z_bwd == pytest.approx(0.0203)
    assert cfg.limits.leash_enabled is True
    assert cfg.limits.leash_z == 1234


def test_startup_without_kim_ini_gives_defaults_and_names_the_file(tmp_path):
    cfg, path, loaded = kcfg.load_startup_config(None)
    assert not loaded
    assert path == kcfg.default_config_path()     # save_calibration will create it
    assert cfg.calibration.um_per_step_z == Config().calibration.um_per_step_z


def test_explicit_config_wins_over_kim_ini(tmp_path):
    _ini_with_z_pair(kcfg.default_config_path())
    other = tmp_path / "other.ini"
    save_config(Config(), str(other))
    cfg, path, loaded = kcfg.load_startup_config(str(other))
    assert loaded and path == other
    assert cfg.calibration.um_per_step_z_bwd == 0.0


def test_loaded_ini_writes_nothing_to_the_controller(tmp_path):
    """The file says 125 V; the controller runs 112 V. Starting must adopt 112
    and send NOTHING -- the file only fills in the calibration and limits."""
    _ini_with_z_pair(kcfg.default_config_path())
    cfg, _path, _ = kcfg.load_startup_config(None)
    backend = SimKim(cfg, state=FOUND)
    brain = Kim(backend, cfg)
    brain.start()
    try:
        brain.status()
        assert backend.writes == []
        assert brain.status().voltage == [0.0, 0.0, 112.0]
        assert cfg.motion.voltage_z == 112.0                  # adopted, not the file's 125
        assert brain.um_per_step(2, +1) == pytest.approx(0.0161)
        assert brain.um_per_step(2, -1) == pytest.approx(0.0203)
    finally:
        brain.shutdown()


def _load_run_service():
    spec = importlib.util.spec_from_file_location(
        "kim001_run_service_under_test", PROJECT / "scripts" / "run_service.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_service_loads_kim_ini_and_saves_back_to_it(tmp_path, monkeypatch):
    """The script itself, not just the helper: started with no --config it
    must load kim001.ini and point the brain's saves at that same file."""
    ini = _ini_with_z_pair(kcfg.default_config_path())
    mod = _load_run_service()
    seen = {}

    class _FakeService:                     # no sockets: just catch the brain
        def __init__(self, brain, **kw):
            seen["brain"] = brain

        def serve_forever(self):
            return None

    monkeypatch.setattr(mod, "Kim001Service", _FakeService)
    monkeypatch.setattr(sys, "argv", ["run_service.py"])
    assert mod.main() == 0
    brain = seen["brain"]
    assert brain.cfg.calibration.um_per_step_z_bwd == pytest.approx(0.0203)
    assert Path(brain.config_file()) == ini


# --------------------------------------------------------------------------- #
# 2. save_calibration merges, round-trips, creates
# --------------------------------------------------------------------------- #
def _brain(cfg: Config | None = None) -> Kim:
    brain, _ = build_sim_system(cfg or Config())
    return brain


def test_save_calibration_keeps_other_sections(tmp_path):
    ini = tmp_path / "kim001.ini"
    ini.write_text("[Limits]\nleash_enabled = True\nleash_xy = 777\n\n"
                   "[Lab]\nnote = keep me\n", encoding="utf-8")
    brain = _brain()
    brain.set_calibration(2, 0.0161, +1)
    brain.set_calibration(2, 0.0203, -1)
    out = brain.save_calibration(ini)
    assert Path(out) == ini
    cp = configparser.ConfigParser()
    cp.read(ini, encoding="utf-8")
    assert cp["Limits"]["leash_xy"] == "777"                # untouched
    assert cp["Lab"]["note"] == "keep me"                   # unknown section kept
    assert float(cp["Calibration"]["um_per_step_z_bwd"]) == pytest.approx(0.0203)
    # ONLY the calibration was written: no Motion / UI section appeared
    assert "Motion" not in cp and "UI" not in cp


def test_save_calibration_round_trip_bwd(tmp_path):
    ini = tmp_path / "kim001.ini"
    brain = _brain()
    brain.set_calibration(2, 0.0161, +1)
    brain.set_calibration(2, 0.0203, -1)
    brain.save_calibration(ini)
    c = load_config(str(ini)).calibration
    assert (c.um_per_step_z, c.um_per_step_z_bwd) == pytest.approx((0.0161, 0.0203))
    # and a fresh brain from that file converts with the same two sizes
    back = _brain(load_config(str(ini)))
    assert back.um_per_step(2, +1) == pytest.approx(0.0161)
    assert back.um_per_step(2, -1) == pytest.approx(0.0203)


def test_save_calibration_creates_missing_file_at_the_default_path(tmp_path):
    ini = kcfg.default_config_path()
    assert not ini.exists()
    brain = _brain()
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.set_calibration(2, 0.0175, 0)
    out = brain.save_calibration()
    assert Path(out) == ini and ini.is_file()
    assert load_config(str(ini)).calibration.um_per_step_z == pytest.approx(0.0175)
    assert any(level == "info" and "saved" in msg for level, msg in events)
    # nothing left behind by the atomic write
    assert [p.name for p in ini.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_save_config_saves_everything_and_keeps_unknown_sections(tmp_path):
    ini = tmp_path / "kim001.ini"
    ini.write_text("[Lab]\nnote = keep me\n", encoding="utf-8")
    cfg = Config()
    cfg.limits.leash_xy = 4321
    cfg.ui.theme = "light"
    brain = _brain(cfg)
    brain.save_config(ini)
    back = load_config(str(ini))
    assert back.limits.leash_xy == 4321 and back.ui.theme == "light"
    cp = configparser.ConfigParser()
    cp.read(ini, encoding="utf-8")
    assert cp["Lab"]["note"] == "keep me"


def test_failed_write_leaves_the_old_file_intact(tmp_path, monkeypatch):
    """Atomic: a crash mid-write must not leave a half file that loads as
    defaults next start (the step sizes would silently be gone again)."""
    ini = tmp_path / "kim001.ini"
    brain = _brain()
    brain.set_calibration(2, 0.0161, 0)
    brain.save_calibration(ini)
    before = ini.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(configparser.ConfigParser, "write", boom)
    brain.set_calibration(2, 0.0999, 0)
    with pytest.raises(OSError):
        brain.save_calibration(ini)
    assert ini.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


# --------------------------------------------------------------------------- #
# 3. over the wire
# --------------------------------------------------------------------------- #
def test_save_verbs_over_the_wire(tmp_path):
    pytest.importorskip("zmq")
    from kim001.net.client import Kim001Client
    from kim001.net.service import Kim001Service

    brain = _brain()
    brain.config_path = tmp_path / "svc" / "kim001.ini"         # folder does not exist yet
    svc = Kim001Service(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = Kim001Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    try:
        cli.set_calibration("Z", 0.0161, +1)
        cli.set_calibration("Z", 0.0203, -1)
        path = cli.save_calibration()
        assert Path(path) == brain.config_path
        c = load_config(path).calibration
        assert (c.um_per_step_z, c.um_per_step_z_bwd) == pytest.approx((0.0161, 0.0203))
        cli.set_leash(leash_xy=2468)
        assert Path(cli.save_config()) == brain.config_path
        assert load_config(path).limits.leash_xy == 2468
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)


# --------------------------------------------------------------------------- #
# 4. GUI buttons
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_step_size_card_save_button_saves_calibration(qapp, tmp_path):
    from kim001.apps.gui import MainWindow

    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    win = MainWindow(brain, cfg, remote=False)
    try:
        brain.set_calibration(2, 0.0161, +1)
        brain.set_calibration(2, 0.0203, -1)
        win._cal_save_btn.click()
        ini = kcfg.default_config_path()
        c = load_config(str(ini)).calibration
        assert (c.um_per_step_z, c.um_per_step_z_bwd) == pytest.approx((0.0161, 0.0203))
    finally:
        win.close()
        brain.shutdown()


def test_settings_save_config_button(qapp, tmp_path, monkeypatch):
    from kim001.apps import gui as gui_mod
    from kim001.apps.settings_dialog import SettingsDialog

    # the dialog's own button: marks the request AND accepts (applies) the edits
    dlg = SettingsDialog(Config())
    dlg._save_btn.click()
    assert dlg.save_requested is True
    assert dlg.result() == 1

    # the main window: after the apply, saves the whole config
    class _SaveDialog:
        save_requested = True

        def __init__(self, cfg, parent=None):
            cfg.limits.leash_xy = 1357            # "edited in the dialog"

        def exec(self):
            return True

    monkeypatch.setattr(gui_mod, "SettingsDialog", _SaveDialog)
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    win = gui_mod.MainWindow(brain, cfg, remote=False)
    try:
        win._open_settings()
        assert load_config(str(kcfg.default_config_path())).limits.leash_xy == 1357
    finally:
        win.close()
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 5. kim001.ini is lab data: never committed
# --------------------------------------------------------------------------- #
def test_kim_ini_is_gitignored():
    git = shutil.which("git")
    if git is None or not (PROJECT / ".gitignore").exists():
        pytest.skip("no git / not a checkout")
    r = subprocess.run([git, "check-ignore", "-q", "kim001.ini"], cwd=PROJECT,
                       capture_output=True)
    if r.returncode == 128:
        pytest.skip("not inside a git work tree")
    assert r.returncode == 0, "kim001.ini is not gitignored"
