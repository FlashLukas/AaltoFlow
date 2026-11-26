"""The fixes from the security review of 2026-11-26 (suite-common side).

* the policy FAILS CLOSED: a PC with a keyring it cannot read right now uses
  its last copy, or encrypts everything -- never "off";
* the keyring is mirrored to this PC, so a share outage neither opens the
  door nor locks every other PC out, and a retired PC stays retired;
* the guard stamps every request "own" / "other" PC from the KEY, whatever
  the client claimed;
* from another PC: no file paths, no change to limits / hardware / interlock
  / file settings (control.path_refusal, control.config_refusal).
"""

from __future__ import annotations

import json
import shutil

import pytest

from suite_common import control, secure


def _key(ch: str) -> str:
    return ch * 40


@pytest.fixture
def pc(tmp_path, monkeypatch):
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


# ------------------------------------------------------------ fail closed --

def test_an_unreachable_keyring_uses_the_last_policy_not_off(pc):
    _, kr = pc
    _policy(kr, "enforce", ["*"])
    assert secure.policy()["mode"] == "enforce"         # read once: copied locally
    shutil.rmtree(kr)                                   # the share is gone (boot, blip)
    assert secure.policy() == {"mode": "enforce", "modules": ["*"]}
    assert secure.module_secured("clmag")


def test_an_unreachable_keyring_with_no_copy_encrypts_everything(pc):
    _, kr = pc
    shutil.rmtree(kr)
    assert secure.policy() == {"mode": "enforce", "modules": ["*"]}


def test_a_broken_policy_file_is_not_off(pc):
    _, kr = pc
    _policy(kr, "enforce", ["kim"])
    secure.policy()
    (kr / secure.POLICY_FILE).write_text("{ not json", encoding="utf-8")
    assert secure.policy()["mode"] == "enforce"


def test_a_readable_keyring_without_a_policy_is_still_off(pc):
    # the lab CHOSE no policy: that is "off", and the copy follows it
    _, kr = pc
    _policy(kr, "enforce", ["*"])
    secure.policy()
    (kr / secure.POLICY_FILE).unlink()
    assert secure.policy()["mode"] == "off"
    shutil.rmtree(kr)
    assert secure.policy()["mode"] == "off"             # the last policy READ


def test_a_pc_never_set_up_is_still_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "nothing"))
    assert secure.policy() == {"mode": "off", "modules": []}


def test_the_keyring_copy_is_used_while_the_share_is_away(pc):
    d, kr = pc
    cache = d / secure.KEYRING_CACHE
    secure.write_cert(kr / "b.key", _key("b"), meta={"pc": "pc-b"})
    secure.write_cert(kr / "c.key", _key("c"), meta={"pc": "pc-c"})
    assert secure.Keyring(kr, cache).by_key(_key("b")) is not None   # mirrored
    (kr / "c.key").unlink()                                          # c RETIRED
    assert secure.Keyring(kr, cache).by_key(_key("c")) is None       # ... and the copy follows
    shutil.rmtree(kr)
    ring = secure.Keyring(kr, cache)
    assert ring.by_key(_key("b")).pc == "pc-b"          # still trusted from the copy
    assert ring.by_key(_key("c")) is None               # a retired PC stays out


def test_without_a_copy_an_unreachable_keyring_trusts_nobody(pc):
    d, kr = pc
    secure.write_cert(kr / "b.key", _key("b"), meta={"pc": "pc-b"})
    shutil.rmtree(kr)
    assert secure.Keyring(kr, d / secure.KEYRING_CACHE).entries() == []


# ----------------------------------------------------------- origin stamp --

def _guard(kr):
    secure.write_cert(kr / "b.key", _key("b"), meta={"pc": "pc-b"})
    secure.write_cert(kr / "o2.key", _key("p"), meta={"pc": "pc-own"})
    return secure.Guard("kim", "enforce", secure.Keyring(kr), _key("o"), "pc-own")


def test_the_guard_stamps_where_a_request_came_from(pc):
    _, kr = pc
    g = _guard(kr)
    own, other, own2 = {"cmd": "x"}, {"cmd": "x", "_origin": "own"}, {"cmd": "x"}
    g.check(own, _key("o"))
    g.check(other, _key("b"))                    # a client's own "_origin" is overwritten
    g.check(own2, _key("p"))                     # the keyring's entry for this PC
    assert (own["_origin"], other["_origin"], own2["_origin"]) == ("own", "other", "own")


# ------------------------------------------------- paths and safety config --

def test_a_path_from_another_pc_is_refused(pc):
    lease = control.ControlLease()
    r = lease.handle({"cmd": "save_positions", "path": r"C:\Users\x\kim.ini",
                      "_origin": "other"})
    assert r and r["refused"] == "own_pc" and "own PC" in r["error"]
    r = lease.handle({"cmd": "save_picture", "folder": r"\\host\share", "_origin": "other"})
    assert r and r["refused"] == "own_pc"
    # an empty folder = the service's default: fine
    assert lease.handle({"cmd": "save_picture", "folder": "", "_origin": "other"}) is None
    # the own PC, or a service that is not encrypted (no stamp): as before
    assert lease.handle({"cmd": "save_positions", "path": "x", "_origin": "own"}) is None
    assert lease.handle({"cmd": "save_positions", "path": "x"}) is None


def test_a_verb_that_confines_its_own_path_may_be_named(pc):
    lease = control.ControlLease(path_ok={"get_file"})
    assert lease.handle({"cmd": "get_file", "path": "2026/x.nc", "_origin": "other"}) is None


CURRENT = {"limits": {"current_max_A": 3.0, "enforce": True},
           "limits_1": {"vpp_max": 5.0},
           "hardware": {"resource": "GPIB0::6::INSTR", "dll_path": ""},
           "calibration": {"px_file": "px.json", "um_per_step": 0.02},
           "pid": {"kc": 0.01}, "ui": {"theme": "dark"}}


def test_another_pc_may_not_change_limits_hardware_or_files():
    for group, k, v in (("limits", "current_max_A", 20.0), ("limits", "enforce", False),
                        ("limits_1", "vpp_max", 10.0),
                        ("hardware", "dll_path", r"\evil\x.dll"),
                        ("calibration", "px_file", r"C:\x.json")):
        req = {"cmd": "set_config", "config": {group: {k: v}}, "_origin": "other"}
        r = control.config_refusal(req, CURRENT)
        assert r and r["refused"] == "own_pc" and f"{group}.{k}" in r["error"], (group, k)


def test_another_pc_may_change_the_rest_and_resend_the_same_values():
    # a remote Settings dialog sends the WHOLE config back
    whole = json.loads(json.dumps(CURRENT))
    whole["pid"]["kc"] = 0.02
    whole["ui"]["theme"] = "light"
    whole["calibration"]["um_per_step"] = 0.03
    whole["limits"]["current_max_A"] = 3          # int for float: the same value
    req = {"cmd": "set_config", "config": whole, "_origin": "other"}
    assert control.config_refusal(req, CURRENT) is None


def test_the_own_pc_may_change_everything():
    req = {"cmd": "set_config", "config": {"limits": {"current_max_A": 20.0}},
           "_origin": "own"}
    assert control.config_refusal(req, CURRENT) is None
    req.pop("_origin")                            # not encrypted: as before
    assert control.config_refusal(req, CURRENT) is None


# ---------------------------------------------------- second tier (same day) --

def test_an_address_from_another_pc_is_refused():
    lease = control.ControlLease()
    for req in ({"cmd": "start_px_calibration", "camera_host": "10.0.0.66"},
                {"cmd": "set_follow", "channel": 1, "enabled": True, "endpoint": "x:5567"}):
        r = lease.handle({**req, "_origin": "other"})
        assert r and r["refused"] == "own_pc" and "address" in r["error"], req
        assert lease.handle({**req, "_origin": "own"}) is None


def test_shutdown_from_another_pc_needs_control_and_never_keeps_outputs():
    lease = control.ControlLease()
    holder = {"id": "h", "kind": "gui", "host": "u@pc-b"}
    other = {"id": "o", "kind": "gui", "host": "u@pc-c"}
    assert lease.handle({"cmd": "take_control", "client": holder})["ok"]
    r = lease.handle({"cmd": "shutdown", "client": other, "_origin": "other"})
    assert r and r["refused"] == "own_pc"
    r = lease.handle({"cmd": "shutdown", "keep_outputs": True, "client": holder,
                      "_origin": "other"})
    assert r and "keep_outputs" in r["error"]
    assert lease.handle({"cmd": "shutdown", "client": holder, "_origin": "other"}) is None
    # the own PC (the launcher's clean stop), with or without keep_outputs
    assert lease.handle({"cmd": "shutdown", "keep_outputs": True, "_origin": "own"}) is None
    assert lease.handle({"cmd": "shutdown"}) is None          # not encrypted: as before


def test_safety_settings_are_recognised_by_path():
    assert control.is_safety_setting(("limits", "current_max_A"))
    assert control.is_safety_setting(("calibration", "px_file"))
    assert not control.is_safety_setting(("motion", "speed"))
    assert not control.is_safety_setting(())


def test_under_enforce_a_client_never_falls_back_to_plain(pc, monkeypatch):
    d, kr = pc
    secure.write_cert(d / secure.OWN_PUBLIC, _key("a"), meta={"pc": "me"})
    secure.write_cert(d / secure.OWN_SECRET, _key("a"), _key("s"), meta={"pc": "me"})
    monkeypatch.setattr(secure, "_flipped", {})
    _policy(kr, "enforce", ["*"])
    assert secure.no_answer("lab-pc", "kim") is False        # no flip to plain
    assert not secure._flipped
    # "off": a service still encrypted from before may be reached encrypted
    _policy(kr, "off", ["*"])
    assert secure.no_answer("lab-pc", "kim") is True


def test_a_service_command_socket_has_a_size_limit(tmp_path, monkeypatch):
    zmq = pytest.importorskip("zmq")
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "nothing"))
    ctx = zmq.Context()
    rep, pub = ctx.socket(zmq.REP), ctx.socket(zmq.PUB)
    try:
        assert secure.secure_server(ctx, [rep, pub], "kim") is None     # plain, but:
        assert rep.maxmsgsize == secure.MAX_MSG_BYTES
    finally:
        rep.close(0); pub.close(0); ctx.term()
