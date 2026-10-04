"""The Security window (security_window.py), offscreen, on a made-up lab.

Every test has its own security folder (AALTOFLOW_SECURITY_DIR) and keyring
in tmp_path -- never the security setup of the PC running the tests. The
window's questions (folder / file dialogs, yes-no) are replaced by answers.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
zmq = pytest.importorskip("zmq")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtWidgets  # noqa: E402

from suite_common import keyadmin as KA  # noqa: E402
from suite_common import secure  # noqa: E402


@pytest.fixture
def app():
    import theme
    theme.set_theme("dark")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def me(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "me"))
    monkeypatch.setattr(secure, "RELOAD_S", 0.0)
    return tmp_path


def _window(answers=None, modules=("kim", "camera", "pm16"), restarted=None):
    import security_window as SW
    log = []
    dlg = SW.SecurityDialog(modules=lambda: list(modules),
                            restart=(restarted.extend if restarted is not None else None),
                            log=lambda m, level="info": log.append((level, m)))
    answers = answers if answers is not None else {}
    dlg.confirm = lambda title, text: answers.setdefault("confirmed", []).append(text) or \
        answers.get("yes", True)
    return dlg, log


def test_opens_on_an_unset_pc_and_says_what_to_do(app, me):
    dlg, _ = _window()
    assert dlg.v_key.text() == "none yet"
    assert dlg.v_ring.text() == "none chosen"
    text = dlg.advice.text()
    assert "Choose keyring folder" in text and "Make this PC's key" in text
    assert "keyring" in dlg.ring_note.text()               # Trusted PCs: no keyring, said
    assert not dlg.btn_add.isEnabled()
    assert dlg.mode_buttons["off"].isChecked()
    assert not dlg.apply_btn.isEnabled()
    dlg.close()


def test_choose_a_new_keyring_then_make_the_key(app, me):
    kr = me / "share" / "keyring"
    kr.mkdir(parents=True)
    dlg, log = _window()
    dlg.pick_dir = lambda start="": str(kr)
    dlg.choose_keyring()                                    # empty folder: "new keyring?" yes
    assert json.loads((kr / "policy.json").read_text(encoding="utf-8"))["mode"] == "off"
    assert secure.keyring_dir() == kr
    dlg.machine_box.setChecked(True)
    dlg.make_key()
    assert dlg.v_key.text().startswith("yes, ")
    assert dlg.v_in.text().startswith("yes, as")
    assert dlg.v_machine.text() == "yes"
    assert dlg.advice.text().startswith("All set")
    assert [dlg.table.item(r, 4).text() for r in range(dlg.table.rowCount())] == ["this PC"]
    # a second key asks first: "no" keeps the old one
    old = secure.own_keys()[0]
    answers = {"yes": False}
    dlg.confirm = lambda t, x: answers.setdefault("c", []).append(x) or answers["yes"]
    dlg.make_key()
    assert secure.own_keys()[0] == old and "stops working everywhere" in answers["c"][0]
    # save the public key for the USB stick
    out = me / "usb" / "pc.key"
    dlg.pick_save = lambda name: str(out)
    dlg.export_key()
    assert secure.read_cert(out)[0] == old and secure.read_cert(out)[1] is None
    dlg.close()


def _lab(me, mode="warn", modules=("kim",)):
    kr = me / "keyring"
    KA.init_keyring(kr, mode, list(modules))
    return kr


def test_add_from_file_shows_a_row_and_machine_box_writes_the_file(app, me):
    kr = _lab(me)
    brought = me / "usb" / "office-1.key"
    secure.write_cert(brought, "b" * 40, meta={"pc": "office-1", "machine": "no"})
    dlg, log = _window()
    dlg.pick_open = lambda: str(brought)
    dlg.ask_add = lambda kf: (kf.pc, False)
    dlg.add_pc()
    assert (kr / "office-1.key").exists()
    assert dlg.table.item(0, 0).text() == "office-1"
    holder = dlg.table.cellWidget(0, 1)
    box = holder.findChild(QtWidgets.QCheckBox)
    assert not box.isChecked()
    box.click()                                             # the user ticks "may run scans"
    assert 'machine = "yes"' in (kr / "office-1.key").read_text(encoding="utf-8")
    assert any("office-1: may run scans (machine) = yes" in m for _, m in log)
    dlg.close()


def test_a_secret_key_file_is_refused_in_words(app, me):
    kr = _lab(me)
    secret = me / "usb" / "this_pc.key_secret"
    secure.write_cert(secret, "b" * 40, "s" * 40, meta={"pc": "office-1"})
    dlg, _ = _window()
    dlg.pick_open = lambda: str(secret)
    dlg.ask_add = lambda kf: pytest.fail("must not ask for a name")
    dlg.add_pc()
    assert "SECRET" in dlg.msg.text()
    assert not list(kr.glob("*.key"))
    dlg.close()


def test_unreadable_key_file_is_a_red_row_with_the_hint(app, me, monkeypatch):
    kr = _lab(me)
    secure.write_cert(kr / "office-1.key", "b" * 40, meta={"pc": "office-1"})
    real = secure.read_cert

    def read(path):
        if Path(path).name == "office-1.key":
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)
    monkeypatch.setattr(secure, "read_cert", read)
    dlg, _ = _window()
    assert dlg.rows == [("problem", "office-1.key")]
    assert "add it again from this PC" in dlg.table.item(0, 1).text()
    assert "cannot be read here" in dlg.ring_note.text()
    dlg.close()


def test_retire_with_confirmation_then_restore(app, me):
    kr = _lab(me)
    secure.write_cert(kr / "old-laptop.key", "c" * 40, meta={"pc": "old-laptop"})
    answers = {}
    dlg, _ = _window(answers)
    assert dlg.select_pc("old-laptop")
    dlg.retire_pc()
    assert "old-laptop will no longer reach any secured module" in answers["confirmed"][0]
    assert not (kr / "old-laptop.key").exists()
    assert list((kr / "retired").glob("old-laptop-*.key"))
    assert dlg.rows[0][0] == "retired"
    dlg.table.selectRow(0)
    assert dlg.btn_restore.isEnabled() and not dlg.btn_retire.isEnabled()
    dlg.restore_pc()
    assert (kr / "old-laptop.key").exists()
    assert dlg.rows == [("pc", "old-laptop")]
    dlg.close()


def test_retire_through_a_real_question_box(app, me, monkeypatch):
    """The real confirm() (QMessageBox.question), answered Yes."""
    kr = _lab(me)
    secure.write_cert(kr / "old-laptop.key", "c" * 40, meta={"pc": "old-laptop"})
    import security_window as SW
    dlg = SW.SecurityDialog()
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QtWidgets.QMessageBox.Yes)
    dlg.select_pc("old-laptop")
    dlg.retire_pc()
    assert asked and not (kr / "old-laptop.key").exists()
    dlg.close()


def test_retiring_this_pc_warns_extra(app, me):
    kr = _lab(me)
    pub, sec = secure.new_keypair()
    d = secure.security_dir()
    secure.write_cert(d / secure.OWN_PUBLIC, pub, meta={"pc": "lab-pc-1"})
    secure.write_cert(d / secure.OWN_SECRET, pub, sec, meta={"pc": "lab-pc-1"})
    secure.write_cert(kr / "lab-pc-1.key", pub, meta={"pc": "lab-pc-1"})
    answers = {"yes": False}
    dlg, _ = _window(answers)
    dlg.select_pc("lab-pc-1")
    dlg.retire_pc()
    assert "is THIS PC" in answers["confirmed"][0]
    assert (kr / "lab-pc-1.key").exists()                     # said no: still there
    dlg.close()


def test_policy_apply_writes_the_file_and_lists_a_stale_service(app, me):
    kr = _lab(me, "warn", ["kim"])
    marker = secure._write_marker("pm16", "off")            # a plain pm16 = this process
    try:
        answers = {}
        restarted = []
        dlg, _ = _window(answers, restarted=restarted)
        assert dlg.mode_buttons["warn"].isChecked() and dlg.some_mods.isChecked()
        dlg.set_choice("enforce", ["*"])
        dlg.apply_policy()
        assert json.loads((kr / "policy.json").read_text(encoding="utf-8")) == \
            {"mode": "enforce", "modules": ["*"]}
        text = answers["confirmed"][0]
        assert "WHOLE lab" in text and "PCs in the keyring" in text and \
            "writable ONLY by the lab's administrator" in text
        assert "pm16" in dlg.stale_lbl.text() and not dlg.restart_btn.isHidden()
        dlg.restart_stale()
        assert restarted == ["pm16"]
        assert dlg.restart_btn.isHidden()
        dlg.close()
    finally:
        marker.unlink()


def test_policy_apply_cancelled_changes_nothing(app, me):
    kr = _lab(me, "warn", ["kim"])
    dlg, _ = _window({"yes": False})
    dlg.set_choice("off", ["kim"])
    dlg.apply_policy()
    assert json.loads((kr / "policy.json").read_text(encoding="utf-8"))["mode"] == "warn"
    dlg.close()


def test_badge_text_and_colour_follow_the_mode(app):
    import security_window as SW
    import theme
    assert SW.badge_text({"mode": "off", "modules": ["*"]}) == "Security: off"
    assert SW.badge_text({"mode": "warn", "modules": ["*"]}) == "Security: warn (all modules)"
    assert SW.badge_text({"mode": "enforce", "modules": ["kim", "camera", "pm16"]}) == \
        "Security: enforce (3 modules)"
    lbl = QtWidgets.QLabel()
    SW.style_badge(lbl, {"mode": "enforce", "modules": ["*"]})
    assert theme.COLORS["ok"] in lbl.styleSheet()
    theme.set_theme("light")                                # read at refresh time
    SW.style_badge(lbl, {"mode": "warn", "modules": ["*"]})
    assert theme.LIGHT["accent"] in lbl.styleSheet()
    theme.set_theme("dark")
