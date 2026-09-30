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
