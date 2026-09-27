"""The REAL backend against a fake serial port that speaks the Elliptec protocol.

No hardware and no pyserial needed: a fake ``serial`` module is put into
sys.modules, so ``EllSerialBus.open()`` builds our FakeSerial instead.  What
this proves is OUR side of the protocol -- packet encoding, reply parsing,
matching replies to mounts by address, and never polling a moving mount.
Whether the ELL14 really answers like this is what the # VERIFY marks are for.
"""

import sys
import types

import pytest

from elliptec.backends.ell_serial import (EllSerialBus, decode_s32, encode_s32,
                                          parse_in_reply)
from elliptec.config import Config

PPR = 143360   # pulses per revolution reported by the fake mounts


class FakeMount:
    def __init__(self, addr, pos=0):
        self.addr = addr
        self.pos = pos
        self.vel = 100


class FakeSerial:
    """Byte-level fake of the ELL14K board with a few mounts on the bus."""

    instances = []

    def __init__(self, port=None, **kw):
        self.port = port
        self.kw = kw
        self.mounts = {"0": FakeMount("0", 1000), "1": FakeMount("1", 2000)}
        self.out = b""
        self.pending = []          # [reads_left, reply_line] (move completions)
        self.sent = []
        self.open = True
        FakeSerial.instances.append(self)

    # --- host -> device --------------------------------------------------- #
    def write(self, data: bytes):
        s = data.decode("ascii")
        self.sent.append(s)
        a, cmd, arg = s[0], s[1:3], s[3:]
        m = self.mounts.get(a)
        if m is None:
            return len(data)                  # nobody on that address: silence
        if cmd == "in":
            self._reply(f"{a}IN0E" + "12345678" + "2024" + "17" + "01" + "0168" + f"{PPR:08X}")
        elif cmd == "gp":
            self._reply(f"{a}PO{encode_s32(m.pos)}")
        elif cmd == "gv":
            self._reply(f"{a}GV{m.vel:02X}")
        elif cmd == "sv":
            if int(arg, 16) > 100:
                self._reply(f"{a}GS04")       # value out of range: speed kept
            else:
                m.vel = int(arg, 16)
                self._reply(f"{a}GS00")
        elif cmd in ("ma", "mr", "ho"):
            if cmd == "ma":
                m.pos = decode_s32(arg)
            elif cmd == "mr":
                m.pos += decode_s32(arg)
            else:
                m.pos = 0
            # the reply comes only when the move is over: after a few reads
            self.pending.append([3, f"{a}PO{encode_s32(m.pos)}"])
        elif cmd == "st":
            self.pending = [p for p in self.pending if not p[1].startswith(a)]
            self._reply(f"{a}GS00")
        return len(data)

    def _reply(self, line):
        self.out += (line + "\r\n").encode("ascii")

    # --- device -> host --------------------------------------------------- #
    def _tick(self):
        for p in list(self.pending):
            p[0] -= 1
            if p[0] <= 0:
                self.pending.remove(p)
                self._reply(p[1])

    @property
    def in_waiting(self):
        self._tick()
        return len(self.out)

    def read(self, n=1):
        self._tick()
        chunk, self.out = self.out[:n], self.out[n:]
        return chunk

    def reset_input_buffer(self):
        self.out = b""

    def close(self):
        self.open = False


@pytest.fixture()
def bus(monkeypatch):
    fake = types.ModuleType("serial")
    fake.Serial = FakeSerial
    fake.EIGHTBITS, fake.PARITY_NONE, fake.STOPBITS_ONE = 8, "N", 1
    monkeypatch.setitem(sys.modules, "serial", fake)
    FakeSerial.instances.clear()
    cfg = Config()
    cfg.hardware.port = "COM99"
    b = EllSerialBus(cfg)
    b.open(["0", "1"])
    yield b, FakeSerial.instances[-1]
    b.close()


def test_hex_helpers():
    assert encode_s32(0x4600) == "00004600"
    assert encode_s32(-1) == "FFFFFFFF"
    assert decode_s32("FFFFFFFF") == -1
    assert decode_s32(encode_s32(-123456)) == -123456


def test_in_reply_parsing():
    info = parse_in_reply("0E" + "12345678" + "2024" + "17" + "01" + "0168" + "00023000")
    assert info["model"] == "ELL14"
    assert info["travel_deg"] == 360
    assert info["pulses_per_rev"] == 143360


def test_open_reads_pulses_from_the_mount_and_uses_9600_8n1(bus):
    b, ser = bus
    assert ser.port == "COM99"
    assert ser.kw["baudrate"] == 9600 and ser.kw["bytesize"] == 8
    assert b.device_info("0")["pulses_per_rev"] == PPR
    r = b.poll("0")
    assert r.device_deg == pytest.approx(1000 / PPR * 360)
    assert "0in" in ser.sent and "1in" in ser.sent


def test_pulses_override(monkeypatch, bus):
    b, _ser = bus
    b.cfg.hardware.pulses_per_rev_override = 1000
    b.open(["0"])
    assert b.device_info("0")["pulses_per_rev"] == 1000


def test_move_abs_encoding_and_completion(bus):
    b, ser = bus
    b.start_move_abs("0", 90.0)
    assert ser.sent[-1] == "0ma" + encode_s32(round(90.0 / 360 * PPR))
    first = b.poll("0")
    assert first.moving
    # while it moves, the driver must NOT ask "gp" (its PO would look like the end)
    n_gp = sum(1 for s in ser.sent if s == "0gp")
    for _ in range(5):
        r = b.poll("0")
    assert not r.moving and r.error_code == 0
    assert r.device_deg == pytest.approx(90.0, abs=0.01)
    assert sum(1 for s in ser.sent if s == "0gp") >= n_gp


def test_negative_relative_move_is_twos_complement(bus):
    b, ser = bus
    b.start_move_rel("1", -10.0)
    assert ser.sent[-1] == "1mr" + encode_s32(round(-10.0 / 360 * PPR))


def test_replies_are_matched_by_address(bus):
    b, _ser = bus
    b.start_move_abs("0", 45.0)
    b.start_move_abs("1", 135.0)
    for _ in range(8):
        r0, r1 = b.poll("0"), b.poll("1")
    assert r0.device_deg == pytest.approx(45.0, abs=0.01)
    assert r1.device_deg == pytest.approx(135.0, abs=0.01)


def test_home_direction_and_stop(bus):
    b, ser = bus
    b.start_home("0", ccw=True)
    assert ser.sent[-1] == "0ho1"
    b.stop("0")
    assert ser.sent[-1] == "0st"
    r = b.poll("0")
    assert not r.moving


def test_velocity_is_hex_percent_and_deferred_while_moving(bus):
    b, ser = bus
    b.set_velocity("0", 60)
    assert ser.sent[-1] == "0sv3C" and b.read_velocity("0") == 60
    b.start_move_abs("0", 10.0)
    b.set_velocity("0", 80)
    assert "0sv50" not in ser.sent          # not while the move is answered by PO
    for _ in range(6):
        b.poll("0")
    assert "0sv50" in ser.sent


def test_move_timeout_reports_mechanical_timeout(bus):
    b, ser = bus
    b.cfg.hardware.move_timeout_s = 0.0
    b.start_move_abs("0", 30.0)
    ser.pending.clear()                     # the mount never answers
    r = b.poll("0")
    assert not r.moving and r.error_code == 2


def test_missing_mount_times_out_on_open(monkeypatch, bus):
    b, _ser = bus
    with pytest.raises(TimeoutError):
        b.open(["7"])


def test_refused_velocity_is_not_reported_as_taken(bus):
    """A GS04 answer to sv must raise, not silently claim the new speed."""
    b, _ser = bus
    b.set_velocity("0", 60)
    with pytest.raises(RuntimeError, match="GS04"):
        b.set_velocity("0", 150)
    assert b.read_velocity("0") == 60


# --------------------------------------------------------------------------- #
# adopt-on-start rule (Lukas, 2026-09-27): open() only QUERIES the mounts
# --------------------------------------------------------------------------- #
def test_open_sends_only_queries_and_adopts_speed_and_angle(monkeypatch):
    """open() may send in / gv / gp and nothing else: no sv (speed), no ho
    (home), no ma/mr (move), no st.  A mount left at 45 % and at a non-zero
    angle is reported as such."""
    fake = types.ModuleType("serial")

    class PreSet(FakeSerial):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.mounts["0"].vel = 45
            self.mounts["1"].vel = 70

    fake.Serial = PreSet
    fake.EIGHTBITS, fake.PARITY_NONE, fake.STOPBITS_ONE = 8, "N", 1
    monkeypatch.setitem(sys.modules, "serial", fake)
    FakeSerial.instances.clear()
    cfg = Config()
    cfg.hardware.port = "COM99"
    cfg.motion.velocity_pct = 100           # the config default must NOT be pushed
    b = EllSerialBus(cfg)
    b.open(["0", "1"])
    try:
        ser = FakeSerial.instances[-1]
        cmds = {s[1:3] for s in ser.sent}
        assert cmds <= {"in", "gv", "gp"}, ser.sent
        assert b.read_velocity("0") == 45 and b.read_velocity("1") == 70
        assert abs(b.poll("0").device_deg - 1000 / PPR * 360) < 1e-9
    finally:
        b.close()
