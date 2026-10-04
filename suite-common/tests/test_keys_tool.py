"""tools/keys.py -- the lab keyring from the command line.

Everything but `new` (which makes a key with pyzmq) runs with plain Python,
so it is tested here; `new` is exercised by kim-control's tests, which have
pyzmq, through the same secure.write_cert.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import keys as K  # noqa: E402

from suite_common import secure  # noqa: E402


@pytest.fixture
def me(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "me"))
    monkeypatch.setattr(secure, "RELOAD_S", 0.0)
    return tmp_path


def test_init_use_policy_machine_remove(me, capsys):
    kr = me / "keyring"
    assert K.main(["init", str(kr), "--mode", "warn", "--modules", "kim,camera"]) == 0
    assert json.loads((kr / "policy.json").read_text(encoding="utf-8")) == \
        {"mode": "warn", "modules": ["kim", "camera"]}
    assert secure.keyring_dir() == kr                  # init also points this PC at it
    with pytest.raises(SystemExit, match="already has"):
        K.main(["init", str(kr)])

    secure.write_cert(kr / "lab-pc-1.key", "a" * 40, meta={"pc": "lab-pc-1", "machine": "no"})
    assert K.main(["machine", "lab-pc-1", "yes"]) == 0
    assert secure.Keyring(kr).by_host("lab-pc-1").machine

    assert K.main(["policy", "--mode", "enforce"]) == 0
    assert secure.policy() == {"mode": "enforce", "modules": ["kim", "camera"]}
    assert K.main(["policy", "--modules", "*"]) == 0
    assert secure.policy()["modules"] == ["*"]

    assert K.main(["list"]) == 0
    assert "lab-pc-1" in capsys.readouterr().out
    assert K.main(["remove", "lab-pc-1"]) == 0
    assert secure.Keyring(kr).entries() == []
    with pytest.raises(SystemExit, match="no PC called"):
        K.main(["remove", "lab-pc-1"])


def test_status_on_a_pc_that_was_never_set_up(me, capsys):
    assert K.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "none yet" in out and "mode 'off'" in out and out.isascii()


def test_list_and_status_say_which_key_files_cannot_be_read(me, capsys, monkeypatch):
    """On the lab share the office PC's key was unreadable from the lab PC and
    `list` just left it out (2026-09-30). Now both commands say so."""
    kr = me / "keyring"
    K.main(["init", str(kr), "--mode", "warn", "--modules", "kim"])
    secure.write_cert(kr / "office.key", "a" * 40, meta={"pc": "office"})
    secure.write_cert(kr / "lab.key", "b" * 40, meta={"pc": "lab"})
    real = secure.read_cert

    def read(path):
        if Path(path).name == "office.key":
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)
    monkeypatch.setattr(secure, "read_cert", read)
    capsys.readouterr()
    assert K.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "1 key file(s) could not be read" in out and "office.key" in out
    assert "lab" in out and out.isascii()
    assert K.main(["status"]) == 0
    assert "office.key: cannot be read (permissions?)" in capsys.readouterr().out


def test_use_warns_about_a_folder_without_a_policy(me, capsys):
    (me / "empty").mkdir()
    assert K.main(["use", str(me / "empty")]) == 0
    assert "no policy.json" in capsys.readouterr().out


def test_policy_names_the_services_still_running_in_the_old_mode(me, capsys):
    """Lab PC, 2026-10-03: the policy went "off" while kim and the camera ran
    encrypted. A service keeps the mode it started with, so say which ones
    on this PC need a restart."""
    import os
    kr = me / "keyring"
    assert K.main(["init", str(kr), "--mode", "enforce", "--modules", "kim,camera"]) == 0
    secure._write_marker("kim", "enforce")                     # this process is "kim"
    dead = secure._write_marker("camera", "enforce")
    data = json.loads(dead.read_text(encoding="utf-8"))
    data["pid"] = 999999999                                    # a camera that has exited
    dead.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()
    assert K.main(["policy", "--mode", "off"]) == 0
    out = capsys.readouterr().out
    assert "restart them" in out and f"kim  (pid {os.getpid()}" in out
    assert "camera" not in out.split("restart them")[1]        # the dead one is not listed
    assert not dead.exists()                                   # ... and its marker is gone
    capsys.readouterr()
    assert K.main(["policy", "--mode", "enforce"]) == 0        # back to its mode: nothing to say
    assert "restart them" not in capsys.readouterr().out


def test_no_answer_switches_only_on_a_pc_with_a_key(me):
    assert secure.no_answer("lab-pc", "kim") is False          # no key: nothing to switch to
    assert secure._flipped == {}


def test_policy_also_names_plain_services_the_new_policy_secures(me, capsys):
    """Lab PC, 2026-10-04: after 'policy --modules "*"' the plain pm16,
    signalhound and dssg were not listed, although they needed a restart --
    only ENCRYPTED services left a marker. Now a plain service leaves one
    too, on a PC that has a keyring (one never set up writes nothing)."""
    kr = me / "keyring"
    assert K.main(["init", str(kr), "--mode", "warn", "--modules", "kim"]) == 0

    class Ctx:                                   # never touched on the plain path
        pass
    assert secure.secure_server(Ctx(), [], "pm16") is None      # plain: pm16 not listed
    capsys.readouterr()
    assert K.main(["policy", "--modules", "*"]) == 0
    out = capsys.readouterr().out
    assert "restart them" in out and "pm16" in out.split("restart them")[1]
    assert "mode 'off'" in out
    capsys.readouterr()
    assert K.main(["policy", "--modules", "kim"]) == 0          # back: pm16 matches again
    assert "restart them" not in capsys.readouterr().out


def test_a_pc_without_a_keyring_writes_no_marker(me):
    class Ctx:
        pass
    assert secure.secure_server(Ctx(), [], "pm16") is None
    assert not (secure.security_dir() / secure.RUNNING_DIR).exists()
