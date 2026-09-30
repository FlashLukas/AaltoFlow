"""suite_common/secure.py without a network: the settings, the policy, the key
files, the keyring and the guard's identity check. (The CurveZMQ handshake
itself is tested where there is a service: kim-control/tests/test_secure.py.)

suite-common has no dependencies, so the keys here are made-up 40-character
strings -- the format is all these tests need.
"""

from __future__ import annotations

import json

import pytest

from suite_common import secure


def _key(ch: str) -> str:
    return ch * 40


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """This PC's security folder, and a keyring next to it."""
    d = tmp_path / "me"
    kr = tmp_path / "keyring"
    d.mkdir()
    kr.mkdir()
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(d))
    monkeypatch.setattr(secure, "RELOAD_S", 0.0)
    (d / secure.SETTINGS_FILE).write_text(json.dumps({"keyring": str(kr)}), encoding="utf-8")
    return d, kr


def _policy(kr, mode, modules):
    (kr / secure.POLICY_FILE).write_text(json.dumps({"mode": mode, "modules": modules}),
                                         encoding="utf-8")


def test_a_pc_that_was_never_set_up_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "nothing"))
    assert secure.keyring_dir() is None
    assert secure.policy() == {"mode": "off", "modules": []}
    assert not secure.module_secured("kim")


def test_the_policy_names_the_secured_modules(pc):
    _, kr = pc
    _policy(kr, "enforce", ["kim", "Camera"])
    assert secure.module_secured("kim")
    assert secure.module_secured("camera")
    assert secure.module_secured("kim@pc-a:5567")      # a remote service in the launcher
    assert secure.module_secured("kim_pc-a")           # ... as the suite spells it
    assert not secure.module_secured("piezo")
    assert not secure.module_secured("kimx")
    _policy(kr, "warn", ["*"])
    assert secure.module_secured("piezo")
    _policy(kr, "off", ["*"])
    assert not secure.module_secured("piezo")
    _policy(kr, "nonsense", ["*"])
    assert secure.policy()["mode"] == "off"            # a typo never half-secures


def test_key_files_round_trip_in_the_zmq_format(tmp_path):
    p = tmp_path / "x.key_secret"
    secure.write_cert(p, _key("a"), _key("b"), meta={"pc": "lab-pc-1", "machine": "yes"})
    public, secret, meta = secure.read_cert(p)
    assert (public, secret) == (_key("a"), _key("b"))
    assert meta == {"pc": "lab-pc-1", "machine": "yes"}
    text = p.read_text(encoding="utf-8")
    assert 'public-key = "' + _key("a") + '"' in text and text.isascii()


def test_a_file_that_is_not_a_key_is_not_trusted(pc):
    _, kr = pc
    (kr / "junk.key").write_text("hello\n", encoding="utf-8")
    secure.write_cert(kr / "pc-b.key", _key("b"), meta={"pc": "pc-b"})
    assert [e.pc for e in secure.Keyring(kr).entries()] == ["pc-b"]


def test_the_keyring_finds_a_pc_by_name_address_or_host(pc):
    _, kr = pc
    secure.write_cert(kr / "a.key", _key("a"), meta={
        "pc": "Lab-PC-1", "host": "desk17", "addresses": "10.0.0.5, lab-pc-1.example.org",
        "machine": "yes"})
    ring = secure.Keyring(kr)
    for host in ("lab-pc-1", "LAB-PC-1", "10.0.0.5", "lab-pc-1.example.org", "desk17",
                 "lab-pc-1.other.domain"):
        e = ring.by_host(host)
        assert e is not None and e.pc == "lab-pc-1" and e.machine, host
    assert ring.by_host("10.0.0.6") is None
    assert ring.by_key(_key("a")).pc == "lab-pc-1"


def test_deleting_a_file_forgets_the_pc(pc):
    _, kr = pc
    secure.write_cert(kr / "b.key", _key("b"), meta={"pc": "pc-b"})
    ring = secure.Keyring(kr)
    assert ring.by_key(_key("b")) is not None
    (kr / "b.key").unlink()
    assert ring.by_key(_key("b")) is None


def test_no_key_yet_says_what_to_do(pc):
    with pytest.raises(secure.SecurityError, match="tools/keys.py new"):
        secure.own_keys()


# ------------------------------------------------------------------- guard --

def _guard(kr, mode="enforce"):
    secure.write_cert(kr / "b.key", _key("b"), meta={"pc": "pc-b", "machine": "no"})
    secure.write_cert(kr / "m.key", _key("m"), meta={"pc": "pc-m", "host": "desk9",
                                                     "machine": "yes"})
    said = []
    g = secure.Guard("kim", mode, secure.Keyring(kr), _key("o"), "pc-own",
                     on_event=lambda level, msg: said.append(msg))
    return g, said


def test_the_guard_admits_keys_in_the_keyring_and_its_own(pc):
    _, kr = pc
    g, said = _guard(kr)
    assert g.callback("d", _key("b").encode())
    assert g.callback("d", _key("o").encode())          # the service's own PC
    assert not g.callback("d", _key("x").encode())
    assert any("not in the keyring" in m for m in said)
    g, said = _guard(kr, mode="warn")
    assert g.callback("d", _key("x").encode())          # warn: in, but said
    assert said


def test_the_guard_checks_the_identity_against_the_key(pc):
    _, kr = pc
    g, _ = _guard(kr)
    ok = {"cmd": "move", "client": {"kind": "gui", "host": "anna@pc-b", "name": "kim GUI"}}
    assert g.check(ok, _key("b")) is None
    # another PC's name
    lie = {"cmd": "move", "client": {"kind": "gui", "host": "anna@pc-m"}}
    r = g.check(lie, _key("b"))
    assert r["refused"] == "security" and "claims to be on 'pc-m'" in r["error"]
    # a machine claim from a PC that may not
    r = g.check({"cmd": "move", "client": {"kind": "machine", "host": "x@pc-b"}}, _key("b"))
    assert r and "may not act as one" in r["error"]
    # ... from one that may, under its host name
    assert g.check({"cmd": "move", "client": {"kind": "machine", "host": "x@desk9"}},
                   _key("m")) is None
    # the service's own PC may act as a machine (the camera next to kim)
    assert g.check({"cmd": "move", "client": {"kind": "machine", "host": "x@pc-own"}},
                   _key("o")) is None
    # no identity claimed: nothing to check (control decides)
    assert g.check({"cmd": "status"}, _key("b")) is None


def test_warn_mode_passes_a_lie_but_says_so_once(pc):
    _, kr = pc
    g, said = _guard(kr, mode="warn")
    lie = {"cmd": "move", "client": {"kind": "machine", "host": "x@pc-b"}}
    assert g.check(lie, _key("b")) is None
    assert g.check(lie, _key("b")) is None
    assert len(said) == 1 and "'warn' lets it through" in said[0]


def test_the_master_is_ascii_and_imports_no_zmq_at_top():
    from pathlib import Path
    text = Path(secure.__file__).read_text(encoding="utf-8")
    assert text.isascii()
    top = [ln for ln in text.splitlines() if ln.startswith(("import ", "from "))]
    assert not any("zmq" in ln for ln in top)
