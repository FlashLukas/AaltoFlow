"""suite_common/keyadmin.py -- the logic behind Mission Control's Security
window and tools/keys.py: this PC's key, the keyring, retiring PCs, the
policy. Everything in tmp folders (AALTOFLOW_SECURITY_DIR), never the real
PC's security setup. PC names are made up.

Plus the one network test: a PC retired while its connection is OPEN is
refused on its next message (CurveZMQ checks a key only at the handshake;
secure.Guard.check looks it up again for every message).
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from suite_common import keyadmin as KA
from suite_common import secure


def _key(ch: str) -> str:
    return ch * 40


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """This PC's security folder ("me") and a lab keyring, policy warn."""
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "me"))
    monkeypatch.setattr(secure, "RELOAD_S", 0.0)
    kr = tmp_path / "keyring"
    KA.init_keyring(kr, "warn", ["kim"])
    return kr


def test_status_of_a_pc_that_was_never_set_up(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "me"))
    st = KA.status()
    assert not st.has_key and st.keyring is None and st.in_keyring is None
    assert st.policy["mode"] == "off"
    advice = " ".join(st.advice())
    assert "Choose keyring folder" in advice and "Make this PC's key" in advice
    assert advice.isascii()


def test_make_key_goes_into_the_keyring_and_refuses_to_replace(lab):
    pytest.importorskip("zmq")
    made = KA.make_key(machine=True, pc="lab-pc-1", addresses=["10.0.0.5"])
    assert made.keyring_file == lab / "lab-pc-1.key" and not made.replaced
    st = KA.status()
    assert st.has_key and st.in_keyring and st.machine and st.keyring_name == "lab-pc-1"
    assert st.public_prefix == made.public[:8]
    assert st.advice() == ["All set: this PC has a key and is in the keyring."]
    with pytest.raises(KA.NeedsOverwrite, match="stops working everywhere"):
        KA.make_key(pc="lab-pc-1")
    again = KA.make_key(pc="lab-pc-1", force=True)
    assert again.replaced and again.public != made.public


def test_make_key_without_a_writable_keyring_leaves_the_file_here(tmp_path, monkeypatch):
    pytest.importorskip("zmq")
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "me"))
    KA.use_keyring(tmp_path / "offline-share")             # not there
    made = KA.make_key(pc="lab-pc-2", here=tmp_path)
    assert made.keyring_file is None and made.local_file == tmp_path / "lab-pc-2.key"
    assert secure.read_cert(made.local_file)[1] is None     # public half only


def test_export_writes_only_the_public_half(lab, tmp_path):
    with pytest.raises(KA.AdminError, match="no key yet"):
        KA.export_public(tmp_path / "x.key")
    d = secure.security_dir()
    secure.write_cert(d / secure.OWN_PUBLIC, _key("a"), meta={"pc": "lab-pc-1"})
    secure.write_cert(d / secure.OWN_SECRET, _key("a"), _key("s"), meta={"pc": "lab-pc-1"})
    out = KA.export_public(tmp_path / "usb" / "lab-pc-1.key")
    public, secret, meta = secure.read_cert(out)
    assert public == _key("a") and secret is None and meta["pc"] == "lab-pc-1"
    assert _key("s") not in out.read_text(encoding="utf-8")


def test_add_from_file_refuses_a_secret_key(lab, tmp_path):
    f = tmp_path / "this_pc.key_secret"
    secure.write_cert(f, _key("b"), _key("s"), meta={"pc": "office-1"})
    with pytest.raises(KA.AdminError, match="SECRET"):
        KA.add_from_file(f)
    assert not list(lab.glob("*.key"))


def test_add_from_file_writes_a_fresh_readable_file(lab, tmp_path):
    f = tmp_path / "brought.key"
    secure.write_cert(f, _key("b"), meta={"pc": "office-1", "addresses": "10.0.0.9"})
    added = KA.add_from_file(f, machine=True)
    assert added.file == lab / "office-1.key" and not added.replaced
    e = secure.Keyring(lab).by_key(_key("b"))
    assert e.pc == "office-1" and e.machine and e.addresses == ("10.0.0.9",)
    # the same PC again: asks first, then replaces
    with pytest.raises(KA.NeedsOverwrite):
        KA.add_from_file(f)
    assert KA.add_from_file(f, machine=False, overwrite=True).replaced
    assert not secure.Keyring(lab).by_key(_key("b")).machine
    # the same key under another name: refused
    with pytest.raises(KA.AdminError, match="already in the keyring as 'office-1'"):
        KA.add_from_file(f, pc_name="office-2")
    # a name that cannot be a file name
    secure.write_cert(f, _key("c"), meta={})
    with pytest.raises(KA.AdminError, match="cannot be a PC name"):
        KA.add_from_file(f, pc_name="bad name/..")


def test_add_replaces_a_key_file_this_pc_could_not_read(lab, tmp_path, monkeypatch):
    """The point of 'add': a key file dropped on the share from another PC can
    be unreadable here; written again from here, it is readable."""
    secure.write_cert(lab / "office-1.key", _key("b"), meta={"pc": "office-1"})
    real = secure.read_cert

    def read(path):
        if path == lab / "office-1.key":
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)
    monkeypatch.setattr(secure, "read_cert", read)
    entries, problems = KA.entries()
    assert entries == [] and "office-1.key: cannot be read (permissions?)" in problems
    f = tmp_path / "office-1-usb.key"
    real(lab / "office-1.key")                               # (sanity: the file is fine)
    secure.write_cert(f, _key("b"), meta={"pc": "office-1"})
    with pytest.raises(KA.NeedsOverwrite):
        KA.add_from_file(f)
    monkeypatch.setattr(secure, "read_cert", real)           # the fresh file is readable
    assert KA.add_from_file(f, overwrite=True).replaced
    assert [e.pc for e in KA.entries()[0]] == ["office-1"]


def test_set_machine_round_trip(lab):
    secure.write_cert(lab / "lab-pc-1.key", _key("a"), meta={"pc": "lab-pc-1", "machine": "no"})
    assert KA.set_machine("lab-pc-1", True) is True
    assert "machine = \"yes\"" in (lab / "lab-pc-1.key").read_text(encoding="utf-8")
    assert secure.Keyring(lab).by_host("lab-pc-1").machine
    KA.set_machine("lab-pc-1", False)
    assert not secure.Keyring(lab).by_host("lab-pc-1").machine
    with pytest.raises(KA.AdminError, match="no PC called"):
        KA.set_machine("nobody", True)


def test_retire_moves_the_key_out_and_restore_brings_it_back(lab):
    secure.write_cert(lab / "old-laptop.key", _key("b"), meta={"pc": "old-laptop"})
    r = KA.retire("old-laptop")
    assert r.file.parent == lab / KA.RETIRED_DIR and r.file.name.startswith("old-laptop-")
    assert not (lab / "old-laptop.key").exists() and r.file.exists()
    # a retired key is NOT trusted: the keyring reads only the top folder
    assert secure.Keyring(lab).by_key(_key("b")) is None
    assert KA.entries()[0] == []
    assert [x.pc for x in KA.retired()] == ["old-laptop"]
    with pytest.raises(KA.AdminError, match="no PC called"):
        KA.retire("old-laptop")
    assert KA.restore("old-laptop") == lab / "old-laptop.key"
    assert secure.Keyring(lab).by_key(_key("b")).pc == "old-laptop"
    assert KA.retired() == []
    with pytest.raises(KA.AdminError, match="no retired key"):
        KA.restore("old-laptop")


def test_retiring_this_pc_needs_force(lab):
    d = secure.security_dir()
    secure.write_cert(d / secure.OWN_PUBLIC, _key("a"), meta={"pc": "lab-pc-1"})
    secure.write_cert(d / secure.OWN_SECRET, _key("a"), _key("s"), meta={"pc": "lab-pc-1"})
    secure.write_cert(lab / "lab-pc-1.key", _key("a"), meta={"pc": "lab-pc-1"})
    with pytest.raises(KA.RetiresThisPC, match="THIS PC"):
        KA.retire("lab-pc-1")
    assert KA.retire("lab-pc-1", force=True).pc == "lab-pc-1"


def test_retire_an_unreadable_file_by_its_name(lab, monkeypatch):
    secure.write_cert(lab / "office-1.key", _key("b"), meta={"pc": "office-1"})
    real = secure.read_cert
    monkeypatch.setattr(secure, "read_cert", lambda p: (_ for _ in ()).throw(
        PermissionError(13, "denied")) if p.name == "office-1.key" else real(p))
    assert KA.retire("office-1").pc == "office-1"
    assert not (lab / "office-1.key").exists()


def test_policy_and_stale_services_both_directions(lab):
    # a plain pm16 (this process) on a PC with a keyring: marker mode "off"
    plain = secure._write_marker("pm16", "off")
    enc = secure._write_marker("kim", "warn")
    try:
        assert KA.stale_services() == []                       # both match "warn, kim"
        new = {"mode": "warn", "modules": ["*"]}
        assert [r["module"] for r in KA.stale_services(new)] == ["pm16"]   # now secured
        off = {"mode": "off", "modules": ["kim"]}
        assert [r["module"] for r in KA.stale_services(off)] == ["kim"]    # now plain
        assert KA.set_policy("enforce", ["kim", "camera"]) == \
            {"mode": "enforce", "modules": ["kim", "camera"]}
        assert json.loads((lab / "policy.json").read_text(encoding="utf-8"))["mode"] == "enforce"
        assert [r["module"] for r in KA.stale_services()] == ["kim"]       # warn -> enforce
        with pytest.raises(KA.AdminError, match="unknown mode"):
            KA.set_policy("strict")
    finally:
        plain.unlink()
        enc.unlink()


def test_describe_policy():
    assert KA.describe_policy({"mode": "off", "modules": ["*"]}) == "off"
    assert KA.describe_policy({"mode": "warn", "modules": ["*"]}) == "warn (all modules)"
    assert KA.describe_policy({"mode": "enforce", "modules": ["a", "b", "c"]}) == \
        "enforce (3 modules)"
    assert KA.describe_policy({"mode": "warn", "modules": ["kim"]}) == "warn (1 module)"


def test_use_keyring_warns_without_a_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "me"))
    (tmp_path / "empty").mkdir()
    assert any("no policy.json" in w for w in KA.use_keyring(tmp_path / "empty"))
    assert secure.keyring_dir() == tmp_path / "empty"
    assert "policy.json" in " ".join(KA.status().advice())


# ------------------------------------------- retiring an OPEN connection ---

PORT = 18977


def _serve(mode, tmp_path, monkeypatch):
    """A secured 'kim' REP on this PC, and a client PC 'pc-b' with its own key
    in the keyring. Returns (client socket factory, stop, said)."""
    zmq = pytest.importorskip("zmq")
    me, kr = tmp_path / "me", tmp_path / "keyring"
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(me))
    monkeypatch.setattr(secure, "RELOAD_S", 0.0)
    KA.init_keyring(kr, mode, ["kim"])
    pub, sec = secure.new_keypair()
    secure.write_cert(me / secure.OWN_PUBLIC, pub, meta={"pc": "pc-a"})
    secure.write_cert(me / secure.OWN_SECRET, pub, sec, meta={"pc": "pc-a"})
    b_pub, b_sec = secure.new_keypair()
    secure.write_cert(kr / "pc-b.key", b_pub, meta={"pc": "pc-b"})

    ctx = zmq.Context.instance()
    rep = ctx.socket(zmq.REP)
    rep.setsockopt(zmq.LINGER, 0)
    said = []
    guard = secure.secure_server(ctx, [rep], "kim", on_event=lambda lv, m: said.append(m))
    rep.bind(f"tcp://127.0.0.1:{PORT}")
    stop = threading.Event()

    def serve():
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not stop.is_set():
            if poller.poll(50):
                frame = rep.recv(copy=False)
                req = json.loads(frame.bytes)
                refused = guard.check(req, secure.user_id(frame))
                rep.send_json(refused or {"ok": True})
    t = threading.Thread(target=serve, daemon=True)
    t.start()

    req = ctx.socket(zmq.REQ)
    req.setsockopt(zmq.LINGER, 0)
    req.setsockopt(zmq.RCVTIMEO, 3000)
    req.curve_secretkey = b_sec.encode()
    req.curve_publickey = b_pub.encode()
    req.curve_serverkey = pub.encode()
    req.connect(f"tcp://127.0.0.1:{PORT}")

    def done():
        stop.set()
        t.join(timeout=2)
        req.close(0)
        rep.close(0)
        secure.release_server(guard)
        for p in (me / secure.RUNNING_DIR).glob("*.json"):
            p.unlink()
        time.sleep(0.1)
    return req, kr, done, said


def test_a_retired_pc_is_refused_on_its_open_connection_in_enforce(tmp_path, monkeypatch):
    req, kr, done, said = _serve("enforce", tmp_path, monkeypatch)
    try:
        req.send_json({"cmd": "status"})
        assert req.recv_json() == {"ok": True}            # in the keyring: works
        KA.retire("pc-b")                                 # while connected
        req.send_json({"cmd": "status"})                  # same socket, same session
        reply = req.recv_json()
        assert reply["ok"] is False and reply["refused"] == "security"
        assert "retired" in reply["error"]
        KA.restore("pc-b")
        req.send_json({"cmd": "status"})
        assert req.recv_json() == {"ok": True}            # restored: works again
    finally:
        done()


def test_a_retired_pc_is_let_through_in_warn_with_one_line(tmp_path, monkeypatch):
    req, kr, done, said = _serve("warn", tmp_path, monkeypatch)
    try:
        req.send_json({"cmd": "status"})
        assert req.recv_json() == {"ok": True}
        KA.retire("pc-b")
        for _ in range(3):
            req.send_json({"cmd": "status"})
            assert req.recv_json() == {"ok": True}
        lines = [m for m in said if "retired" in m]
        assert len(lines) == 1 and "'warn' lets it through" in lines[0]
    finally:
        done()

