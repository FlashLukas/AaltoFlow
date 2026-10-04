"""Encryption and who-is-who on the wire (src/kim/secure.py, CurveZMQ).

A small lab is built in a temp folder: a keyring with a policy, and three
"PCs" -- each is just its own security folder (AALTOFLOW_SECURITY_DIR):

  pc-a   runs the kim service; in the keyring; may act as a machine
  pc-b   a GUI PC; in the keyring; may NOT act as a machine
  pc-c   a stranger: has a key, but it is not in the keyring

The service is started "on" pc-a (the environment says pc-a while it
starts), and each client is made "on" its PC the same way. A client reaches
pc-a through 127.0.0.2, which the keyring lists as one of pc-a's addresses
(127.0.0.1 would mean "this PC" to the client).

Wire tests use ports 18950-18959.
"""

from __future__ import annotations

import json
import time

import pytest

zmq = pytest.importorskip("zmq")

from kim import secure  # noqa: E402
from kim.config import Config  # noqa: E402
from kim.sim_system import build_sim_system  # noqa: E402

CMD, PUB = 18950, 18951
TO_A = "127.0.0.2"        # how the other PCs reach pc-a


def _pc(folder, keyring, name, *, machine=False, addresses=(), in_keyring=True,
        host=None):
    """One PC: its own security folder with a key, pointed at the keyring."""
    folder.mkdir(parents=True)
    public, secret = secure.new_keypair()
    meta = {"pc": name, "host": host or name, "machine": "yes" if machine else "no"}
    if addresses:
        meta["addresses"] = " ".join(addresses)
    secure.write_cert(folder / secure.OWN_PUBLIC, public, meta=meta)
    secure.write_cert(folder / secure.OWN_SECRET, public, secret, meta=meta)
    if in_keyring:
        secure.write_cert(keyring / f"{name}.key", public, meta=meta)
    (folder / secure.SETTINGS_FILE).write_text(json.dumps({"keyring": str(keyring)}),
                                               encoding="utf-8")
    return folder


@pytest.fixture
def lab(tmp_path, monkeypatch):
    kr = tmp_path / "keyring"
    kr.mkdir()
    pcs = {
        "a": _pc(tmp_path / "pc-a", kr, "pc-a", machine=True, addresses=(TO_A,)),
        # pc-b is this real PC as far as its host name goes: the console
        # (a subprocess) puts the real name into its identity
        "b": _pc(tmp_path / "pc-b", kr, "pc-b", host=secure.this_pc_name()),
        "c": _pc(tmp_path / "pc-c", kr, "pc-c", in_keyring=False),
    }
    monkeypatch.setattr(secure, "RELOAD_S", 0.0)      # see a keyring change at once
    monkeypatch.setattr(secure, "_flipped", {})       # no mode remembered from another test

    class Lab:
        keyring = kr

        def policy(self, mode, modules=("kim",)):
            (kr / secure.POLICY_FILE).write_text(
                json.dumps({"mode": mode, "modules": list(modules)}), encoding="utf-8")

        def on(self, pc):
            """Everything made from now on is made "on" this PC."""
            monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(pcs[pc]))

    lab = Lab()
    lab.policy("enforce")
    return lab


@pytest.fixture
def start_kim(lab):
    """Start the kim service on pc-a; returns it (stopped at the end)."""
    from kim.net.service import KimService
    made = []

    def start():
        lab.on("a")
        brain, _ = build_sim_system(Config())
        s = KimService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
        # listen on every loopback address, so 127.0.0.2 reaches it too
        s.host = "0.0.0.0"
        s.start()
        # what the guard tells the clients (the publisher drains the event
        # queue at once, so listen at the guard itself)
        s.seen = []
        if s._guard is not None:
            s._guard.on_event = lambda level, msg: s.seen.append(msg)
        made.append(s)
        return s
    yield start
    for s in made:
        s.stop()
    time.sleep(0.2)                                  # let the ports go


@pytest.fixture
def client(lab):
    from kim.net.client import KimClient
    made = []

    def make(pc, kind="gui", host=TO_A, start=True):
        lab.on(pc)
        c = KimClient(host=host, cmd_port=CMD, pub_port=PUB, timeout_ms=1500,
                      kind=kind, name=f"kim {kind} on pc-{pc}")
        c.identity["host"] = f"user@pc-{pc}"
        if start:
            c.start()          # info, config, heartbeat, and the SUB thread
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def _events(svc) -> list[str]:
    """What the service's guard said (it goes to every client's log)."""
    return list(svc.seen)


def _wait(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------ enforce --

def test_a_pc_in_the_keyring_works_as_before(start_kim, client):
    start_kim()
    b = client("b")
    assert b.take_control()
    b.move_to_step(0, 500)
    # telemetry arrives (encrypted, over SUB): the control state it carries
    assert _wait(lambda: (b.control() or {}).get("holder") is not None)


def test_a_stranger_gets_no_answer_at_all(start_kim, client):
    svc = start_kim()
    c = client("c", start=False)
    with pytest.raises(TimeoutError):
        c.take_control()
    assert any("not in the keyring" in m for m in _events(svc))


def test_a_client_without_security_gets_no_answer(start_kim, client, lab, tmp_path,
                                                  monkeypatch):
    start_kim()
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "nothing"))
    from kim.net.client import KimClient
    plain = KimClient(host=TO_A, cmd_port=CMD, pub_port=PUB, timeout_ms=1000)
    try:
        with pytest.raises(TimeoutError):
            plain.take_control()
    finally:
        plain.close()


def test_a_pc_may_not_claim_to_be_a_machine(start_kim, client):
    start_kim()
    b = client("b", kind="machine")
    with pytest.raises(RuntimeError, match="refused \\(security\\).*may not act as one"):
        b.move_to_step(0, 100)


def test_the_services_own_pc_may_act_as_a_machine(start_kim, client):
    start_kim()
    a = client("a", kind="machine", host="127.0.0.1")    # the camera next to kim
    a.move_to_step(0, 100)                               # no refusal


def test_a_pc_may_not_claim_to_be_another_pc(start_kim, client):
    start_kim()
    b = client("b")
    b.identity["host"] = "user@pc-a"          # pretends to sit at kim's own PC
    with pytest.raises(RuntimeError, match="claims to be on 'pc-a' but its key is pc-b's"):
        b.take_control()


def test_a_real_identity_beats_the_control_lock_only_when_it_is_true(start_kim, client):
    """The point of it all: before, a viewer could send kind "machine" and pass
    the lock. Now pc-b holds control, and pc-c cannot pretend its way past."""
    start_kim()
    b = client("b")
    assert b.take_control()
    b2 = client("b", kind="machine")          # same PC, but not allowed as machine
    with pytest.raises(RuntimeError, match="refused \\(security\\)"):
        b2.move_to_step(0, 100)


def test_removing_a_key_from_the_keyring_locks_that_pc_out(start_kim, client, lab):
    start_kim()
    b = client("b", start=False)
    assert b.take_control()
    b.release_control()
    (lab.keyring / "pc-b.key").unlink()
    b2 = client("b", start=False)             # a new connection: a new handshake
    with pytest.raises(TimeoutError):
        b2.take_control()


def test_telemetry_is_not_readable_without_a_key(start_kim, lab):
    start_kim()
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.RCVTIMEO, 1200)
    sub.setsockopt(zmq.LINGER, 0)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(f"tcp://{TO_A}:{PUB}")                   # plain: no key
    try:
        with pytest.raises(zmq.Again):
            sub.recv_multipart()
    finally:
        sub.close(0)


def test_an_impostor_service_is_not_believed(lab, client):
    """A service on pc-a's address, but with pc-c's key: the client knows pc-a's
    key from the keyring, so the handshake fails and nothing is answered."""
    lab.on("c")
    public, secret, _ = secure.own_keys()
    ctx = zmq.Context.instance()
    rep = ctx.socket(zmq.REP)
    rep.curve_secretkey, rep.curve_publickey = secret.encode(), public.encode()
    rep.curve_server = True
    rep.setsockopt(zmq.LINGER, 0)
    rep.bind(f"tcp://0.0.0.0:{CMD}")
    try:
        b = client("b", start=False)
        with pytest.raises(TimeoutError):
            b.take_control()
    finally:
        rep.close(0)


def test_a_client_is_told_when_the_keyring_lacks_the_pc(lab, client):
    with pytest.raises(secure.SecurityError, match="no key for '10.9.9.9'"):
        client("b", host="10.9.9.9", start=False)


# --------------------------------------------------------------- warn / off --

def test_warn_mode_lets_everything_through_and_says_so(start_kim, client, lab):
    lab.policy("warn")
    svc = start_kim()
    c = client("c", start=False)              # a stranger
    assert c.take_control()
    c.release_control()
    b = client("b", kind="machine", start=False)
    b.move_to_step(0, 10)                     # a false machine claim, let through
    msgs = _events(svc)
    assert any("not in the keyring connected" in m for m in msgs)
    assert any("may not act as one" in m and "'warn' lets it through" in m for m in msgs)


def test_a_module_not_in_the_policy_stays_plain(start_kim, lab, tmp_path, monkeypatch):
    lab.policy("enforce", modules=("camera",))
    start_kim()
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "nothing"))
    from kim.net.client import KimClient
    plain = KimClient(host=TO_A, cmd_port=CMD, pub_port=PUB, timeout_ms=1500)
    try:
        assert plain.take_control()
    finally:
        plain.close()


def test_the_console_speaks_curve_when_the_policy_asks(start_kim, lab):
    import subprocess
    import sys
    from pathlib import Path
    start_kim()
    lab.on("b")
    import os
    script = Path(__file__).resolve().parents[1] / "scripts" / "kim_console.py"
    r = subprocess.run([sys.executable, str(script), "--host", TO_A, "--port", str(CMD),
                        "status"], capture_output=True, text=True, timeout=20,
                       env=dict(os.environ))
    assert '"ok": true' in r.stdout, r.stdout + r.stderr


# ------------------------------------------- the policy changes under a service --
#
# Found on the lab PC (2026-10-03): `keys.py policy --mode off` while kim ran
# encrypted -> every client spoke plain, got no answer, and not even a
# shutdown reached the service. A service picks its mode when it STARTS; a
# client that hears nothing now tries the other mode once.

def test_policy_switched_off_while_kim_runs_encrypted(start_kim, client, lab):
    svc = start_kim()                       # encrypted (enforce)
    lab.policy("off")
    b = client("b", start=False)            # a new GUI / script / the launcher's Stop
    assert b.take_control()                 # one timeout, then encrypted, as the service
    assert b.take_control()                 # and it stays so (no second timeout)
    assert svc._guard is not None


def test_policy_switched_on_while_kim_runs_plain(start_kim, client, lab):
    lab.policy("off")
    svc = start_kim()                       # plain
    assert svc._guard is None
    lab.policy("enforce")
    b = client("b", start=False)
    assert b.take_control()                 # one timeout, then plain, as the service


def test_telemetry_follows_the_mode_a_request_found(start_kim, client, lab):
    start_kim()
    lab.policy("off")
    b = client("b", start=False)
    b.start()                               # its first request finds the encrypted mode
    assert b.take_control()
    assert _wait(lambda: (b.control() or {}).get("holder") is not None)


def test_after_a_restart_the_client_follows_back(start_kim, client, lab):
    svc = start_kim()
    lab.policy("off")
    b = client("b", start=False)
    assert b.take_control()                 # encrypted, against the policy
    svc.stop()
    time.sleep(0.3)
    start_kim()                             # restarted: now plain, as the policy says
    assert b.take_control()                 # one timeout, then back to plain


def test_a_pc_without_a_key_never_switches(start_kim, lab, tmp_path, monkeypatch):
    start_kim()                             # encrypted
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(tmp_path / "nothing"))
    from kim.net.client import KimClient
    plain = KimClient(host=TO_A, cmd_port=CMD, pub_port=PUB, timeout_ms=800)
    try:
        with pytest.raises(TimeoutError):
            plain.take_control()
        assert not secure._flipped          # nothing remembered: it cannot encrypt
    finally:
        plain.close()


def test_a_running_encrypted_service_is_listed_until_it_stops(start_kim, lab):
    svc = start_kim()
    lab.on("a")
    running = secure.running_secured()
    assert [r["module"] for r in running] == ["kim"]
    svc.stop()
    assert secure.running_secured() == []


def test_a_request_long_after_the_policy_change_does_not_hang(start_kim, client, lab):
    """The socket's handshake was refused (wrong mode) before the first send:
    the send must time out (SNDTIMEO) and switch, not wait forever."""
    start_kim()
    lab.policy("off")
    b = client("b", start=False)
    time.sleep(1.0)                         # the plain handshake is refused meanwhile
    assert b.take_control()
