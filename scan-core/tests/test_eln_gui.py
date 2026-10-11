"""The eLabFTW parts of the GUI, offscreen, against the fake eLabFTW of
test_eln.py: the Settings card, the Catalogue's "Send to eLabFTW", and the
after-scan auto-upload that must never break a scan."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6 import QtCore, QtWidgets                                   # noqa: E402

from scan_core import eln                                               # noqa: E402
from test_eln import GOOD_KEY, FakeElab                                 # noqa: E402
from test_catalogue import _write                                       # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def fake():
    f = FakeElab()
    yield f
    f.close()


@pytest.fixture()
def keyfile(tmp_path, monkeypatch):
    """The key store is a temp file: no test touches the real Credential Manager."""
    monkeypatch.setattr(eln, "_keyring", lambda: None)
    monkeypatch.setattr(eln, "_wincred", lambda: None)
    monkeypatch.setattr(eln, "_key_file", lambda: tmp_path / "keystore" / "eln_key")
    monkeypatch.delenv(eln.KEY_ENV, raising=False)
    return tmp_path / "keystore" / "eln_key"


def _pump(cond, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        time.sleep(0.01)
    return cond()


def test_settings_card_saves_tests_and_hides_the_key(qapp, fake, keyfile, tmp_path):
    from apps.eln_settings import ElnSettingsCard
    root = tmp_path / "root"; root.mkdir()
    log = []
    card = ElnSettingsCard(root=root, on_log=log.append)
    assert card.key_edit.echoMode() == QtWidgets.QLineEdit.Password
    assert not card.auto_box.isChecked() and not card.attach_box.isChecked()
    assert "Warning" in card.store_lbl.text()            # the plain-file fallback says so
    card.url_edit.setText(fake.url)
    card.url_edit.editingFinished.emit()
    card.auto_box.setChecked(True)
    s = eln.ElnSettings.load(root)
    assert s.url == fake.url and s.auto_upload

    # a wrong key, typed but not saved: the test says 401
    card.key_edit.setText("wrong")
    card.test(); card.wait_test()
    assert "401" in card.test_lbl.text()
    # the right one, saved: stored, field cleared, never logged
    card.key_edit.setText(GOOD_KEY)
    card.save_key()
    assert card.key_edit.text() == "" and eln.get_api_key() == GOOD_KEY
    assert "stored" in card.key_edit.placeholderText()
    card.test(); card.wait_test()
    assert "Test Person" in card.test_lbl.text()
    assert not any(GOOD_KEY in m for m in log)
    assert GOOD_KEY not in (root / "suite_local.json").read_text(encoding="utf-8")
    card.forget_key()
    assert eln.get_api_key() is None


def test_catalogue_send_to_eln(qapp, fake, keyfile, tmp_path):
    from apps.catalogue_view import ELN_COL, CatalogueWidget
    data = tmp_path / "data"
    _write(data, "2026-10-01/100000_a.nc", name="run a", created="2026-10-01T10:00:00")
    _write(data, "2026-10-02/100000_b.nc", name="run b", created="2026-10-02T10:00:00")
    root = tmp_path / "root"; root.mkdir()
    eln.ElnSettings(url=fake.url).save(root)
    eln.set_api_key(GOOD_KEY)
    log = []
    w = CatalogueWidget(data, on_log=log.append, settings_root=root)
    w.rescan(); w.wait_scan()
    assert _pump(lambda: w.table.topLevelItemCount() == 2)

    # the context menu offers it, for every selected run
    w.table.selectAll()
    menu = w.context_menu_for(w.table.topLevelItem(0))
    texts = [a.text() for a in menu.actions()]
    assert "Send 2 runs to eLabFTW" in texts and "Open in eLabFTW" not in texts

    assert w.send_to_eln(w.selected_runs())
    w.wait_eln()
    assert _pump(lambda: "experiment" in w.eln_lbl.text())
    assert "<a href=" in w.eln_lbl.text() and w.eln_lbl.openExternalLinks()
    cols = sorted(w.table.topLevelItem(i).text(ELN_COL) for i in range(2))
    assert cols == ["#42", "#43"]
    assert sum("created experiment" in m for m in log) == 2
    menu = w.context_menu_for(w.table.topLevelItem(0))
    assert "Open in eLabFTW" in [a.text() for a in menu.actions()]

    # again: it ASKS; "skip" sends nothing, cancel starts nothing
    asked = []
    w._ask_duplicates = lambda already: asked.append(len(already)) or "skip"
    n = len(fake.requests)
    w.table.selectAll()                   # the refresh after an upload rebuilt the table
    assert w.send_to_eln(w.selected_runs()); w.wait_eln()
    assert asked == [2] and len(fake.requests) == n
    w._ask_duplicates = lambda already: None
    w.table.selectAll()
    assert not w.send_to_eln(w.selected_runs())


def test_catalogue_send_without_settings_says_why(qapp, keyfile, tmp_path):
    from apps.catalogue_view import CatalogueWidget
    data = tmp_path / "data"
    _write(data, "2026-10-01/100000_a.nc", name="run a")
    root = tmp_path / "root"; root.mkdir()
    w = CatalogueWidget(data, settings_root=root)
    w.rescan(); w.wait_scan()
    _pump(lambda: w.table.topLevelItemCount() == 1)
    assert not w.send_to_eln(w.rows)
    assert "no eLabFTW address" in w.eln_lbl.text()


def test_auto_upload_failure_does_not_break_the_scan(qapp, fake, keyfile, tmp_path, monkeypatch):
    """The server fails (500) on every creation: the scan still finishes, its
    file is saved, and the failure is a line in the log."""
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    from apps.suite import Suite
    root = tmp_path / "root"; root.mkdir()
    data = tmp_path / "data"; data.mkdir()
    eln.ElnSettings(url=fake.url, auto_upload=True).save(root)
    eln.set_api_key(GOOD_KEY)
    fake.fail[("POST", r"/experiments$")] = 500
    win = Suite(root=root, out_dir=data)
    try:
        b = win.builder
        b.ask_before_unsaved = False
        b.add_axis("field")
        b.rows[0].num.setValue(3)
        b.per_pt.setValue(0.0)
        b.run_scan()
        assert b.worker is not None
        assert b.worker.wait(30000)
        assert _pump(lambda: b.dataset is not None and b.last_saved is not None)
        assert b.last_saved.exists()
        assert _pump(lambda: getattr(win, "_eln_auto", None) is not None)
        win._eln_auto.join(15)
        assert _pump(lambda: "failed" in win.logbox.toPlainText()
                     and "eLabFTW" in win.logbox.toPlainText())
        assert "server error (500)" in win.logbox.toPlainText()
        assert b.run_btn.isEnabled()                    # the run is over normally
        assert ("POST", "/api/v2/experiments") in fake.calls()
    finally:
        win.close()
