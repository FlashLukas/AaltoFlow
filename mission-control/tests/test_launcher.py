"""The launcher against a throwaway suite folder, offscreen.

AALTOFLOW_ROOT points discovery at a temporary root BEFORE mission_control is
imported (the module reads the root once, at import). A tiny ZeroMQ REP thread
plays a running service that answers `describe`, so the remote-service path is
tested over a real socket, not a mock.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

zmq = pytest.importorskip("zmq")
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parents[1]


def _module(root: Path, folder, key, cmd, gui=True, order=10):
    d = root / folder
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "run_service.py").write_text("")
    if gui:
        (d / "scripts" / "run_gui.py").write_text("")
    (d / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
        '<circle cx="20" cy="20" r="10" fill="#ff9e2c"/></svg>')
    (d / "module.toml").write_text(
        f'[module]\nkey = "{key}"\nname = "{key} module"\ndescription = "fake"\n'
        f'icon = "icon.svg"\norder = {order}\n[ports]\ncmd = {cmd}\npub = {cmd + 1}\n'
        f'[run]\nservice = "scripts/run_service.py"\n'
        f'gui = "{"scripts/run_gui.py" if gui else ""}"\n')


class FakeService:
    """Answers `describe` like a module called `key`."""

    def __init__(self, port, key="lockin"):
        self.port, self.key = port, key
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        time.sleep(0.1)

    def _run(self):
        rep = zmq.Context.instance().socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.port}")
        poller = zmq.Poller(); poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(50):
                msg = rep.recv_json()
                if msg.get("cmd") == "describe":
                    rep.send_json({"ok": True, "describe": {
                        "module": self.key, "label": "Fake lock-in", "revision": 1,
                        "parameters": [
                            {"id": "tc1", "label": "Ch1 time constant", "kind": "control",
                             "type": "float", "unit": "ms", "min": 0.01, "max": 500},
                            {"id": "r1", "label": "Ch1 R", "kind": "indicator",
                             "type": "float", "unit": "V"},
                            {"id": "acquire", "label": "Acquire", "kind": "action",
                             "type": "action"}]}})
                else:
                    rep.send_json({"ok": False, "error": "unknown"})
        rep.close(0)

    def stop(self):
        self._stop.set()
        self._t.join(1)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("suite")
    _module(root, "magnet-control", "magnet", 16100, order=10)
    _module(root, "lockin-control", "lockin", 16110, order=20)
    _module(root, "focus-control", "focus", 16120, gui=False, order=30)
    os.environ["AALTOFLOW_ROOT"] = str(root)
    sys.path.insert(0, str(HERE))
    import mission_control as mc
    from PySide6 import QtWidgets
    mc.set_theme("dark")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = mc.MainWindow()
    yield mc, win, root, app
    win.close()


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def test_cards_come_from_the_folder(env):
    mc, win, root, app = env
    assert list(win.cards) == ["magnet", "lockin", "focus"]
    assert not win.cards["focus"].btn_gui.isEnabled()          # headless
    assert win.cards["magnet"].btn_service.isEnabled()


def test_measurement_suite_button_is_not_a_module_card(env):
    """scan-core has no service and no ports, so it is a button, not a card --
    and it says so instead of failing silently when it is not there."""
    mc, win, root, app = env
    assert "scan-core" not in win.cards and "scan_core" not in win.cards
    assert win.suite_btn.isEnabled()

    win.open_suite()                       # the throwaway root has no scan-core
    assert win.suite_proc is None
    assert "no measurement suite at" in win.logbox.toPlainText()
    win.open_viewer()                      # the data viewer lives in scan-core too
    assert win.viewer_proc is None
    assert "no data viewer at" in win.logbox.toPlainText()


def test_a_new_module_folder_appears_on_rescan(env):
    mc, win, root, app = env
    _module(root, "vna-control", "vna", 16130, order=40)
    win.rescan(force=False)                                     # signature changed
    assert "vna" in win.cards
    import shutil
    shutil.rmtree(root / "vna-control")
    win.rescan(force=False)
    assert "vna" not in win.cards


def test_port_override_updates_the_card(env):
    mc, win, root, app = env
    mc.set_ports("magnet", 16200, 16201, root)
    win.rescan(force=True)
    card = win.cards["magnet"]
    assert (card.spec.cmd, card.spec.pub) == (16200, 16201)
    assert "changed" in card.meta.text()
    assert mc.service_args(card.spec)[:2] == ["--cmd-port", "16200"]
    mc.set_ports("magnet", None, None, root)
    win.rescan(force=True)
    assert win.cards["magnet"].spec.cmd == 16100


def test_remote_service_add_shows_variables_and_remove(env):
    mc, win, root, app = env
    svc = FakeService(16300, key="lockin")
    try:
        rid = mc.add_remote("127.0.0.1", 16300, 16301, "lockin", name="Lock-in lab 2", root=root)
        win.rescan(force=True)
        card = win.cards[rid]
        assert card.spec.remote and not card.btn_service.isVisible()
        assert card.spec.has_gui                                  # uses the local twin's GUI
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and card.manifest is None:
            _pump(app, 0.1)
        assert card.up, "the probe never saw the fake service"
        assert card.manifest and card.manifest["module"] == "lockin"
        assert card.btn_vars.text() == "Variables 2"              # 1 control + 1 measured
        assert card.vars.topLevelItemCount() == 3                 # controls, measured, actions
        assert (root / ".suite_cache").is_dir()                   # remembered for later
        mc.remove_remote(rid, root)
        win.rescan(force=True)
        assert rid not in win.cards
    finally:
        svc.stop()


def test_endpoints_are_handed_to_spawned_processes(env):
    mc, win, root, app = env
    table = json.loads(mc.endpoints_json(win.found.modules))
    assert table["lockin"] == ["localhost", 16110, 16111]


def test_profiles_keep_members_that_are_missing_today(env):
    mc, win, root, app = env
    cleaned = mc._clean_profiles([{"name": "Run", "members": ["magnet", "not-here"]},
                                  {"name": mc.FULL_SUITE, "members": []}])
    assert cleaned == [{"name": "Run", "members": ["magnet", "not-here"]}]


def test_icon_follows_the_theme(env):
    mc, win, root, app = env
    spec = win.found.get("magnet")
    pm = mc.module_icon(spec)
    assert not pm.isNull() and pm.width() == 40


# ─────────────────────────── Add module… (2026-09-24) ─────────────────────────

def test_add_module_from_a_folder(env, tmp_path):
    """Pick a folder of modules, see what each would do here, install the new one;
    the launcher shows its card without a restart."""
    mc, win, root, app = env
    shop = tmp_path / "shop"
    _module(shop, "pm-control", "pm", 16110, order=40)        # lockin's ports -> moved
    _module(shop, "magnet-control", "magnet", 16100)          # same folder: update
    _module(shop, "other-control", "lockin", 16200)           # key used elsewhere
    (shop / "pm-control" / "module.toml").write_text(
        (shop / "pm-control" / "module.toml").read_text().replace(
            "[module]\n", '[module]\ncategory = "detector"\ntags = ["power meter"]\n'))

    dlg = win.add_module()
    try:
        dlg.open_source(shop)
        rows = {dlg.tree.topLevelItem(i).data(0, mc.QtCore.Qt.UserRole):
                dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())}
        assert rows["pm"].text(1) == "Detectors & analyzers"
        assert rows["pm"].text(2).startswith("new (ports ")      # its ports were taken
        assert rows["magnet"].text(2) == "update"
        assert rows["lockin"].text(2) == "cannot install" and rows["lockin"].isDisabled()
        assert dlg.checked_keys() == ["pm"]                       # only NEW is pre-ticked

        dlg.cat_combo.setCurrentIndex(dlg.cat_combo.findData("detector"))
        assert dlg.tree.topLevelItemCount() == 1                  # "what do you need?"
        dlg.cat_combo.setCurrentIndex(0)

        dlg.install_checked(build=False)
        _pump(app, 0.2)
        assert (root / "pm-control" / "module.toml").is_file()
        assert "pm" in win.cards                                  # rescanned
        assert win.cards["pm"].spec.cmd not in (16100, 16110, 16120)
        assert mc.port_conflicts(win.found.modules) == []
    finally:
        dlg.close()


def test_add_module_runs_the_build_steps_in_order(env, tmp_path, monkeypatch):
    """The environment build is a chain of processes; a failing step stops that
    module's remaining steps and is reported, and the dialog is closable again."""
    mc, win, root, app = env
    shop = tmp_path / "shop"
    _module(shop, "scope-control", "scope", 16300)
    py = sys.executable
    fake = lambda folder, r, uv, offline: [                        # noqa: E731
        mc.catalog_Step("scope: one", [py, "-c", "print('step one')"], folder),
        mc.catalog_Step("scope: two", [py, "-c", "import sys; sys.exit(3)"], folder),
        mc.catalog_Step("scope: three", [py, "-c", "print('never')"], folder)]
    monkeypatch.setattr(mc, "env_steps", fake)
    monkeypatch.setattr(mc, "find_uv", lambda: "uv")
    dlg = win.add_module()
    try:
        dlg.open_source(shop)
        dlg.install_checked(build=True)
        assert not dlg.btn_close.isEnabled()                       # busy
        end = time.monotonic() + 20
        while dlg._proc is not None and time.monotonic() < end:
            _pump(app, 0.05)
        text = dlg.logbox.toPlainText()
        assert "step one" in text and "exit code 3" in text and "never" not in text
        assert "FAILED for scope" in text
        assert dlg.btn_close.isEnabled()
    finally:
        dlg.close()


def _serve(folder):
    import http.server
    import threading as th
    from functools import partial
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"


def _wait(app, cond, seconds=15):
    end = time.monotonic() + seconds
    while not cond() and time.monotonic() < end:
        _pump(app, 0.05)
    assert cond(), "timed out"


def test_add_module_from_the_online_catalog(env, tmp_path):
    """The GitHub-release path, against a local HTTP server holding the same
    files a release has: catalog.json + one pack per module."""
    import json
    import zipfile
    from suite_common import catalog as K
    from suite_common.modules import discover_local
    mc, win, root, app = env
    src = tmp_path / "src"
    _module(src, "gauss-control", "gauss", 16400, order=5)
    (src / "gauss-control" / "module.toml").write_text(
        (src / "gauss-control" / "module.toml").read_text().replace(
            "[module]\n", '[module]\ncategory = "field"\ntags = ["gaussmeter"]\n'))
    rel = tmp_path / "release"
    rel.mkdir()
    zpath = rel / "gauss-1.0-abc.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in (src / "gauss-control").rglob("*"):
            zf.write(p, p.relative_to(src).as_posix())
    doc = K.release_catalog(discover_local(src)[0], {"gauss": {"code": zpath}},
                            "abc1234", "modules-test", None)
    assert "icon_svg" in doc["modules"][0]                  # the picture travels along
    (rel / "catalog.json").write_text(json.dumps(doc), encoding="utf-8")
    httpd, url = _serve(rel)
    dlg = win.add_module()
    try:
        dlg.open_catalog(url + "catalog.json")
        _wait(app, lambda: dlg.catalog is not None)
        assert "modules-test" in dlg.src_lbl.text()
        it = dlg.tree.topLevelItem(0)
        assert (it.text(0), it.text(1), it.text(2)) == ("gauss module", "Magnetic field", "new")
        assert dlg.checked_keys() == ["gauss"]
        assert dlg.build_lbl.text().startswith("download 0.0 MB")   # a tiny fake pack

        dlg.install_checked(build=False)
        _wait(app, lambda: not dlg.busy and "gauss" in win.cards)
        log = dlg.logbox.toPlainText()
        assert "verified (SHA-256)" in log and "files copied" in log
        assert (root / "gauss-control" / "module.toml").is_file()
        assert dlg.tree.topLevelItem(0).text(2) == "update"     # re-planned: now installed
    finally:
        dlg.close()
        httpd.shutdown()


def test_an_unreachable_catalog_is_explained_not_raised(env):
    mc, win, root, app = env
    dlg = win.add_module()
    try:
        dlg.open_catalog("http://127.0.0.1:9/catalog.json")
        _wait(app, lambda: not dlg.busy)
        assert dlg.catalog is None and "module pack" in dlg.problems.text()
        assert dlg.tree.topLevelItemCount() == 0
    finally:
        dlg.close()


def _running_proc(app, seconds):
    """A QProcess that is alive for `seconds` -- plays our just-started service."""
    from PySide6 import QtCore
    p = QtCore.QProcess()
    p.start(sys.executable, ["-c", f"import time; time.sleep({seconds})"])
    assert p.waitForStarted(5000)
    return p


def test_gui_right_after_service_waits_and_connects(env, monkeypatch):
    """Pressing GUI while our service is still starting must NOT open a private
    simulator: it waits for the port and then opens the GUI connected."""
    mc, win, root, app = env
    card = win.cards["magnet"]
    launched = []
    monkeypatch.setattr(card, "_launch_gui", lambda connect: launched.append(connect))
    card.up = False
    card.service_proc = _running_proc(app, 6)
    try:
        card.open_gui()
        _pump(app, 0.6)
        assert launched == []                       # still waiting: port closed
        svc = FakeService(16100, key="magnet")      # the service starts listening
        try:
            _pump(app, 1.5)
        finally:
            svc.stop()
        assert launched == [True]                   # opened, and connected
    finally:
        card.service_proc.kill(); card.service_proc.waitForFinished(3000)
        card.service_proc = None


def test_gui_does_not_open_when_the_service_dies_first(env, monkeypatch):
    mc, win, root, app = env
    card = win.cards["magnet"]
    launched = []
    monkeypatch.setattr(card, "_launch_gui", lambda connect: launched.append(connect))
    card.up = False
    card.service_proc = _running_proc(app, 0.5)
    card.open_gui()
    _pump(app, 2.0)
    assert launched == [] and not card._gui_waiting
    card.service_proc = None


def test_export_then_import_settings(env, tmp_path):
    """Export writes one zip; import restores a file tuned since, keeps a
    backup of what it replaced, and the cards follow an imported
    suite_local.json (here: the magnet's real-hardware flag)."""
    mc, win, root, app = env
    ini = root / "magnet-control" / "magnet.ini"
    local = root / mc.LOCAL_FILE
    had_local = local.exists()
    old_local = local.read_bytes() if had_local else None
    try:
        ini.write_text("[hall]\noffset = 1\n", encoding="utf-8")
        mc.set_real("magnet", True, root)
        bundle = win.export_settings(str(tmp_path / "settings.zip"))
        assert bundle is not None and bundle.is_file()
        assert "exported" in win.logbox.toPlainText()

        ini.write_text("[hall]\noffset = 2\n", encoding="utf-8")
        mc.set_real("magnet", False, root)
        win.rescan(force=True)
        assert not win.cards["magnet"].spec.real

        plan = win.import_settings(str(bundle), confirm=False)
        assert {e.path for e in plan.overwrite} == {"magnet-control/magnet.ini",
                                                    mc.LOCAL_FILE}
        assert "offset = 1" in ini.read_text(encoding="utf-8")
        assert win.cards["magnet"].spec.real                  # the card followed
        backups = list((root / ".suite_cache").glob("settings-backup-*.zip"))
        assert backups
        assert "backed up in" in win.logbox.toPlainText()
    finally:
        ini.unlink(missing_ok=True)
        if had_local:
            local.write_bytes(old_local)
        else:
            local.unlink(missing_ok=True)
        win.rescan(force=True)


def test_import_of_a_non_bundle_is_refused(env, tmp_path, monkeypatch):
    mc, win, root, app = env
    bad = tmp_path / "not.zip"
    bad.write_text("hello", encoding="utf-8")
    shown = []
    monkeypatch.setattr(mc.QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: shown.append(a[2]))
    assert win.import_settings(str(bad)) is None
    assert shown and "not a zip" in shown[0]
