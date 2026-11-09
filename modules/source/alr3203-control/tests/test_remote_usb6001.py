"""The polarity relay over usb6001-control's wire protocol, against a FAKE
usb6001 service (a REP socket on a test port): set_do with the right line and
level, the level read back from status, the machine identity, refusals and a
silent service. Offline; ports 17012/17013 (this module's test block)."""

import threading

import pytest
import zmq

from alr3203.backends.remote_usb6001 import Usb6001Relay

PORT = 17012


class FakeUsb6001:
    """Answers status / set_do like usb6001-control, line p0.0 an output."""

    def __init__(self, port=PORT):
        self.dio = [None] * 13
        self.dio_dir = ["out"] + ["unused"] * 12
        self.requests = []
        self.refuse = False
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.REP)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.bind(f"tcp://127.0.0.1:{port}")
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        poller = zmq.Poller()
        poller.register(self._sock, zmq.POLLIN)
        while not self._stop.is_set():
            if not poller.poll(50):
                continue
            req = self._sock.recv_json()
            self.requests.append(req)
            if req.get("cmd") == "status":
                rep = {"ok": True, "status": {"dio": list(self.dio),
                                              "dio_dir": list(self.dio_dir),
                                              "hw_error": ""}}
            elif req.get("cmd") == "set_do":
                if self.refuse:
                    rep = {"ok": False, "error": "p0.0 is configured as 'in'"}
                else:
                    self.dio[0] = bool(req["state"])
                    rep = {"ok": True, "state": self.dio[0]}
            else:
                rep = {"ok": False, "error": "unknown"}
            self._sock.send_json(rep)
        self._sock.close(0)

    def stop(self):
        self._stop.set()
        self._t.join(2)


@pytest.fixture
def daq():
    d = FakeUsb6001()
    yield d
    d.stop()


def test_set_drives_the_line_with_the_configured_level(daq):
    r = Usb6001Relay(cmd_port=PORT, line="p0.0", inverted_level="high")
    r.open()
    assert r.read() is None                    # never written: level unknown
    r.set(True)
    assert daq.dio[0] is True and r.read() is True
    r.set(False)
    assert daq.dio[0] is False and r.read() is False
    req = [q for q in daq.requests if q["cmd"] == "set_do"][0]
    assert req["line"] == "p0.0"
    assert req["client"]["kind"] == "machine"   # switches even if a person holds control
    r.close()


def test_low_level_can_mean_inverted(daq):
    r = Usb6001Relay(cmd_port=PORT, inverted_level="low")
    r.set(True)
    assert daq.dio[0] is False and r.read() is True
    r.close()


def test_a_line_that_is_not_an_output_reads_unknown(daq):
    daq.dio_dir[0] = "in"
    daq.dio[0] = True
    assert Usb6001Relay(cmd_port=PORT).read() is None


def test_a_refusal_raises(daq):
    daq.refuse = True
    r = Usb6001Relay(cmd_port=PORT)
    with pytest.raises(RuntimeError, match="configured as"):
        r.set(True)
    r.close()


def test_a_silent_service_times_out_and_reads_unknown():
    r = Usb6001Relay(cmd_port=PORT + 30, timeout_ms=200)   # nobody listens there
    with pytest.raises(TimeoutError, match="usb6001-control"):
        r.set(True)
    assert r.read() is None
    r.close()


def test_bad_settings_are_refused_at_construction():
    with pytest.raises(ValueError):
        Usb6001Relay(line="p7.9")
    with pytest.raises(ValueError):
        Usb6001Relay(inverted_level="sideways")
