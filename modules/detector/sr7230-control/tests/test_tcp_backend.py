"""The real backend's WIRE FORMAT, against a fake 7230 on loopback.

No instrument needed: a tiny TCP server on 127.0.0.1:17230 answers the way the
manual (section 6.5.05) says port 50000 does -- reply text, NUL, status byte,
overload byte. It keeps a log of the commands it received, so the tests can
check the exact strings the backend sends, and it can be told to split a reply
across TCP packets, which is how the framing bugs would show up in the lab.
"""

import math
import socket
import threading

import pytest

from sr7230.backends.tcp7230 import Tcp7230, InstrumentError, ST_INVALID, ST_UNLOCK
from sr7230.config import Config
from sr7230.lockin import LockIn

PORT = 17230


class Fake7230:
    """Answers each NUL-terminated command from a table of canned replies."""

    def __init__(self):
        self.log: list[str] = []
        self.replies = {"ID": "7230", "VER": "2.20", "XY.": "+1.5E-03,-2.0E-04",
                        "FRQ.": "+1.0000E+03", "ADC. 1": "+1.250", "ADC. 2": "-0.500",
                        "TC.": "+1.0E-01", "SEN": "24", "REFP.": "+30.00",
                        # the front panel as someone left it (read at start)
                        "REFMODE": "0", "IE": "0", "OF.": "+1.0000E+03",
                        "OA.": "+2.500E-01", "REFN": "1", "IMODE": "0", "VMODE": "3",
                        "DCCOUPLE": "1", "FET": "0", "FLOAT": "0", "AUTOMATIC": "1",
                        "FASTMODE": "0", "SLOPE": "3", "LF": "1,1"}
        self.status = 1                  # command complete
        self.overload = 0
        self.split = False               # send each reply in two packets
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", PORT))
        self._srv.listen(1)
        self._stop = False
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        self._srv.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(0.2)
                buf = b""
                while not self._stop:
                    try:
                        chunk = conn.recv(1024)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\x00" in buf:
                        cmd, buf = buf.split(b"\x00", 1)
                        text = cmd.decode()
                        self.log.append(text)
                        status = self.status
                        if text.split()[0] == "BOGUS":
                            status |= ST_INVALID
                        out = self.replies.get(text, "").encode() + b"\x00" + \
                            bytes([status, self.overload])
                        if self.split:
                            conn.sendall(out[:2])
                            conn.sendall(out[2:])
                        else:
                            conn.sendall(out)

    def close(self):
        self._stop = True
        self._srv.close()
        self._t.join(timeout=2)


@pytest.fixture
def fake():
    f = Fake7230()
    yield f
    f.close()


@pytest.fixture
def dev(fake):
    d = Tcp7230("127.0.0.1", port=PORT, timeout_s=2.0)
    d.open()
    yield d
    d.close()


#: every command the backend may send while STARTING: queries only (a command
#: without its argument reports the value on the 7230)
START_QUERIES = {"ID", "VER", "REFMODE", "IE", "OF.", "OA.", "REFP.", "REFN", "IMODE",
                 "VMODE", "DCCOUPLE", "FET", "FLOAT", "AUTOMATIC", "SEN", "FASTMODE",
                 "TC.", "SLOPE", "LF"}


def test_open_identifies_and_only_reads_the_reference_mode(fake, dev):
    assert "7230" in dev.idn() and "2.20" in dev.idn()
    assert fake.log[:3] == ["ID", "VER", "REFMODE"]      # read, never "REFMODE 0"


def test_open_refuses_a_dual_mode_instrument_instead_of_switching_it(fake):
    fake.replies["REFMODE"] = "1"
    d = Tcp7230("127.0.0.1", port=PORT, timeout_s=2.0)
    with pytest.raises(InstrumentError, match="single reference"):
        d.open()
    assert set(fake.log) <= START_QUERIES


def test_read_settings_sends_queries_only_and_parses(fake, dev):
    st = dev.read_settings()
    assert set(fake.log) <= START_QUERIES, set(fake.log) - START_QUERIES
    assert st["osc_amplitude_V"] == pytest.approx(0.25) and st["vmode"] == 3
    assert st["dc_coupled"] is True and st["slope_index"] == 3
    assert st["line_filter_mode"] == 1 and st["line_50Hz"] is True
    assert st["time_constant_s"] == pytest.approx(0.1) and st["unread"] == []


def test_a_query_the_instrument_refuses_is_listed_not_fatal(fake, dev):
    del fake.replies["AUTOMATIC"]            # answers with no number
    st = dev.read_settings()
    assert "auto_ac_gain" not in st and st["unread"] == ["auto_ac_gain"]


def test_setters_send_the_manual_commands(fake, dev):
    n0 = len(fake.log)
    dev.set_ref_source(2)
    dev.set_osc_frequency(1234.5)
    dev.set_osc_amplitude(0.25)
    dev.set_phase(-12.5)
    dev.set_harmonic(2)
    dev.set_input(0, 3)
    dev.set_coupling(dc=True)
    dev.set_sensitivity_index(18)
    dev.set_fast_mode(True)
    dev.set_tc_index(12)
    dev.set_slope_index(3)
    dev.set_line_filter(3, True)
    log = fake.log[n0:]
    assert log == ["IE 2", "OF. 1.234500E+03", "OA. 2.500000E-01", "REFP. -12.5000",
                   "REFN 2", "IMODE 0", "VMODE 3", "DCCOUPLE 1", "SEN 18",
                   "FASTMODE 1", "TC 12", "SLOPE 3", "LF 3 1"]


def test_readings_parse_and_carry_the_status_bytes(fake, dev):
    fake.status = 1 | ST_UNLOCK
    fake.overload = 0b01
    r = dev.read_outputs()
    assert r["x"] == pytest.approx(1.5e-3) and r["y"] == pytest.approx(-2.0e-4)
    assert r["freq_Hz"] == pytest.approx(1000.0)
    assert r["adc"] == [pytest.approx(1.25), pytest.approx(-0.5)]
    assert r["status"] & ST_UNLOCK and r["overload"] == 0b01
    assert dev.get_time_constant() == pytest.approx(0.1)
    assert dev.get_sensitivity_index() == 24
    assert dev.get_phase() == pytest.approx(30.0)


def test_skipping_the_adc_saves_two_round_trips(fake, dev):
    n = len(fake.log)
    r = dev.read_outputs(read_adc=False)
    assert fake.log[n:] == ["XY.", "FRQ."]
    assert all(math.isnan(v) for v in r["adc"])


def test_framing_survives_split_packets(fake, dev):
    fake.split = True
    assert dev.read_outputs()["x"] == pytest.approx(1.5e-3)


def test_a_zero_status_byte_is_not_mistaken_for_the_terminator(fake, dev):
    # status 0 and overload 0 are both NUL bytes on the wire; they must be
    # COUNTED after the terminator, not searched for
    fake.status = 0
    assert dev.read_outputs(read_adc=False)["y"] == pytest.approx(-2.0e-4)
    assert dev.get_sensitivity_index() == 24


def test_an_invalid_command_raises(fake, dev):
    with pytest.raises(InstrumentError, match="invalid"):
        dev._q("BOGUS 1")


def test_no_address_is_refused_before_connecting():
    with pytest.raises(ValueError, match="IP address"):
        Tcp7230("", port=PORT)
    with pytest.raises(ValueError, match="not implemented"):
        Tcp7230("10.0.0.1", interface="serial")


def test_the_brain_drives_the_fake_end_to_end(fake):
    """The brain + real backend: start sends QUERIES ONLY and adopts what the
    instrument reports (OSC OUT keeps its 0.25 V); a deliberate Apply pushes;
    shutdown still sends OA. 0 (osc_off_on_shutdown, not part of the rule)."""
    cfg = Config()
    li = LockIn(Tcp7230("127.0.0.1", port=PORT), cfg)
    li.start(poll=False)
    li.poll_once()
    assert set(fake.log) <= START_QUERIES | {"XY.", "FRQ.", "ADC. 1", "ADC. 2"}, \
        set(fake.log) - START_QUERIES
    s = li.status()
    assert s.connected and s.live["x"] == pytest.approx(1.5e-3) and s.tc_s == pytest.approx(0.1)
    assert s.amplitude_V == pytest.approx(0.25) and s.input == "A-B"
    assert s.coupling == "DC" and s.slope_db == 24 and s.phase_deg == pytest.approx(30.0)
    assert li.cfg.signal.line_filter == "1f"
    # an explicit Apply is a request: THEN everything is pushed, OSC OUT last
    n0 = len(fake.log)
    li.apply_config()
    pushed = [c.split()[0] for c in fake.log[n0:]]
    assert pushed.index("OA.") > pushed.index("TC") and pushed.index("OA.") > pushed.index("SEN")
    li.shutdown()
    assert fake.log[-1] == "OA. 0.000000E+00"
