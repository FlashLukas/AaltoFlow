"""The camera's link to kim over CurveZMQ (src/camera/secure.py).

When the lab's policy secures kim, the camera (KimLink) must reach it
encrypted, with kim's key from the keyring, and kim must let it act as a
"machine" only when the keyring allows the camera's PC to. The kim service
here is a stand-in that runs the SAME guard (secure.py is byte-identical in
every module) and answers ok -- the real kim is another package.

Ports 18960/18961.
"""

from __future__ import annotations

import json
import threading

import pytest

zmq = pytest.importorskip("zmq")

from camera import secure  # noqa: E402

CMD, PUB = 18960, 18961
TO_KIM = "127.0.0.2"          # how the camera's PC reaches kim's PC


def _pc(folder, keyring, name, *, machine, addresses=()):
    folder.mkdir(parents=True)
    public, secret = secure.new_keypair()
    meta = {"pc": name, "host": name, "machine": "yes" if machine else "no"}
    if addresses:
        meta["addresses"] = " ".join(addresses)
    secure.write_cert(folder / secure.OWN_PUBLIC, public, meta=meta)
    secure.write_cert(folder / secure.OWN_SECRET, public, secret, meta=meta)
    secure.write_cert(keyring / f"{name}.key", public, meta=meta)
    (folder / secure.SETTINGS_FILE).write_text(json.dumps({"keyring": str(keyring)}),
                                               encoding="utf-8")
    return folder


class _StandInKim:
    """A REP socket with kim's guard: answers {"ok": true}, or the refusal."""

    def __init__(self):
        self.ctx = zmq.Context.instance()
        self.rep = self.ctx.socket(zmq.REP)
        self.pub = self.ctx.socket(zmq.PUB)
        self.guard = secure.secure_server(self.ctx, [self.rep, self.pub], "kim")
        for s in (self.rep, self.pub):
            s.setsockopt(zmq.LINGER, 0)
        self.rep.bind(f"tcp://0.0.0.0:{CMD}")
        self.pub.bind(f"tcp://0.0.0.0:{PUB}")
        self.seen = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        poller = zmq.Poller()
        poller.register(self.rep, zmq.POLLIN)
        while not self._stop.is_set():
            if dict(poller.poll(100)):
                frame = self.rep.recv(copy=False)
                req = json.loads(frame.bytes)
                self.seen.append(req)
                refused = self.guard.check(req, secure.user_id(frame))
                self.rep.send_json(refused or {"ok": True, "status": {}})

    def close(self):
        self._stop.set()
        self._t.join(timeout=2)
        self.rep.close(0)
        self.pub.close(0)
        secure.release_server(self.guard)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    kr = tmp_path / "keyring"
    kr.mkdir()
    (kr / secure.POLICY_FILE).write_text(json.dumps({"mode": "enforce", "modules": ["kim"]}),
                                         encoding="utf-8")
    pcs = {"kim": _pc(tmp_path / "kim-pc", kr, "kim-pc", machine=False, addresses=(TO_KIM,)),
           "cam": _pc(tmp_path / "cam-pc", kr, "cam-pc", machine=True),
           "gui": _pc(tmp_path / "gui-pc", kr, "gui-pc", machine=False)}

    def on(pc):
        monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(pcs[pc]))
    on("kim")
    kim = _StandInKim()
    yield on, kim
    kim.close()


def _link(on, pc):
    from camera.backends.remote_kim import KimLink
    on(pc)
    link = KimLink(host=TO_KIM, cmd_port=CMD, pub_port=PUB)
    link.identity["host"] = f"camera@{pc}-pc"
    return link


def test_the_camera_drives_a_secured_kim_as_a_machine(lab):
    on, kim = lab
    link = _link(on, "cam")
    try:
        assert link.rpc(cmd="move_to_step", axis=0, position=100)["ok"]
        assert kim.seen[-1]["client"]["kind"] == "machine"
    finally:
        link.close()


def test_a_pc_the_keyring_does_not_allow_as_machine_is_refused(lab):
    on, kim = lab
    link = _link(on, "gui")                 # the camera software on a GUI-only PC
    try:
        with pytest.raises(RuntimeError, match="may not act as one"):
            link.rpc(cmd="move_to_step", axis=0, position=100)
    finally:
        link.close()


def test_links_to_modules_the_policy_does_not_secure_stay_plain(lab):
    on, _ = lab
    on("cam")
    sock = zmq.Context.instance().socket(zmq.REQ)
    try:
        assert secure.secure_client(sock, TO_KIM, "piezo") is False   # piezo: plain
        assert secure.secure_client(sock, TO_KIM, "kim") is True
    finally:
        sock.close(0)
