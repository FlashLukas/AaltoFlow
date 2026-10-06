"""The Siglent backend against a fake SDS1000CML+ (fake_visa.py)."""

import numpy as np
import pytest

from scope.backends.siglent import SiglentSDS, parse_block, _num
from scope.config import Config
from scope.scope import Scope

from fake_visa import fake_visa  # noqa: F401  (pytest fixture)

RES = "USB0::0xF4EC::0xEE3A::SDS00000000001::INSTR"


def test_reply_parsing():
    assert _num("C1:VDIV 5.00E-01V") == 0.5
    assert _num("SARA 1.00E+05Sa/s") == 1e5
    assert _num("INR 8193") == 8193
    raw = b"C1:WF DAT2,#9000000004" + bytes([0, 1, 255, 128]) + b"\n\n"
    assert list(parse_block(raw)) == [0, 1, -1, -128]
    with pytest.raises(ValueError):
        parse_block(b"C1:WF DAT2,#9000000009" + b"\x00" * 3)


def test_open_and_read_settings_only_read(fake_visa):
    b = SiglentSDS(RES)
    b.open()
    s = b.read_settings()
    inst = fake_visa[0]
    assert inst.writes == [], "open/read_settings must not write"
    assert s["unread"] == []
    assert s["channels"]["ch2"] == {"enabled": True, "vdiv_V": 0.1, "offset_V": -0.5,
                                    "coupling": "ac", "probe": 10.0}
    assert s["tdiv_s"] == 5e-3 and s["sample_rate_Hz"] == 1e5
    assert s["trigger"] == {"source": "ext", "level_V": 0.5, "slope": "rising",
                            "mode": "normal"}
    assert b.capabilities()["model"] == "SDS1102CML+"
    b.close()
    assert inst.ren == [6] and inst.closed


def test_setters_send_the_guide_commands(fake_visa):
    b = SiglentSDS(RES)
    b.open()
    b.set_channel("ch1", enabled=False, probe=10, coupling="ac", vdiv_V=0.2, offset_V=-0.1)
    b.set_timebase(tdiv_s=1e-3, delay_s=2e-4)
    b.set_trigger(source="ch2", level_V=0.3, slope="falling", mode="auto")
    w = fake_visa[0].writes
    assert w[:5] == ["C1:TRA OFF", "C1:ATTN 10", "C1:CPL A1M", "C1:VDIV 2.0000E-01V",
                     "C1:OFST -1.0000E-01V"]
    assert w[5:7] == ["TDIV 1.0000E-03S", "TRDL 2.0000E-04S"]
    # a new source keeps the holdoff the scope was set to (TRSE carries it)
    assert w[7:] == ["TRSE EDGE,SR,C2,HT,TI,HV,100NS", "C2:TRLV 3.0000E-01V", "C2:TRSL NEG",
                     "TRMD AUTO"]
    s = b.read_settings()
    assert s["trigger"]["source"] == "ch2" and s["trigger"]["slope"] == "falling"
    b.close()


def test_traces_volts_time_and_thinning(fake_visa):
    b = SiglentSDS(RES)
    b.open()
    assert b.new_trace_ready() is True
    assert b.new_trace_ready() is False               # read-and-clear
    t, v = b.read_traces(["ch1", "ch2"], max_points=2000)
    inst = fake_visa[0]
    assert "WFSU SP,7,NP,0,FP,0" in inst.writes        # 14000 points -> every 7th
    assert t.size == v["ch1"].size == 2000
    # codes +-50 at 0.5 V/div: +-1 V on CH1; CH2 at 0.1 V/div and offset -0.5: 0.5 +- 0.2
    assert np.max(v["ch1"]) == pytest.approx(1.0, abs=0.03)
    assert np.mean(v["ch2"]) == pytest.approx(0.5, abs=0.01)
    # 14000 points at 100 kSa/s = 140 ms, trigger in the centre
    assert t[0] == pytest.approx(-0.07) and t[1] - t[0] == pytest.approx(7e-5)
    b.close()


def test_the_brain_on_the_siglent_backend(fake_visa):
    import time
    cfg = Config()
    cfg.hardware.poll_s = 0.001
    scope = Scope(SiglentSDS(RES), cfg)
    scope.start()
    inst = fake_visa[0]
    try:
        # nothing that changes the SCOPE: only waveform transfers (WFSU thins
        # the transfer, WF? asks for the data) may have gone out
        settings = [w for w in inst.writes if not w.startswith(("WFSU", "C1:WF?", "C2:WF?"))]
        assert settings == [], "start must only read"
        assert cfg.channel_2.coupling == "ac" and cfg.channel_2.probe == 10.0
        scope.set_averages(2)
        n = scope.acquire()
        t_end = time.monotonic() + 5
        while time.monotonic() < t_end:
            inst.st["INR"] = 1                         # the scope keeps triggering
            st = scope.status()
            if st["acq_id"] == n and not st["acquiring"]:
                break
            time.sleep(0.005)
        assert st["sample"]["acq_id"] == n
        assert st["sample"]["ch1"]["pk2pk"] == pytest.approx(2.0, abs=0.05)
    finally:
        scope.shutdown()


# ---- the lab-PC bug of 2026-10-06 (RSDS1102CML+, firmware 6.01.01.25) ----------

def test_a_newline_byte_inside_the_block_does_not_cut_the_read(fake_visa):
    """BUG 1: the int8 code 10 is the byte 0x0A. Read with the text terminator
    on, the block was cut there and every later reply came out of step
    (settings read wrong, "no number in ''"). The block is read with the
    terminator off; the replies after it must be clean."""
    b = SiglentSDS(RES)
    b.open()
    inst = fake_visa[0]
    t, v = b.read_traces(["ch1", "ch2"], max_points=20000)
    assert v["ch1"].size == inst.st["SANU"] and v["ch2"].size == inst.st["SANU"]
    assert not inst._out, "bytes of the block left in the queue"
    assert inst.read_termination == "\n"              # restored for text replies
    s = b.read_settings()                             # the replies after it: clean
    assert s["unread"] == []
    assert s["channels"]["ch1"]["enabled"] is True and s["channels"]["ch1"]["vdiv_V"] == 0.5
    assert s["trigger"]["source"] == "ext"
    b.close()


def test_the_old_way_reproduces_the_desync(fake_visa):
    """The fake really reproduces the bug: with the terminator on, the read
    stops inside the block and the next query gets block bytes."""
    b = SiglentSDS(RES)
    b.open()
    inst = fake_visa[0]
    inst.write("C1:WF? DAT2")
    first = inst.read_raw()                           # terminator "\n" still on
    assert len(first) < 1000 and inst._out            # cut short, rest queued
    with pytest.raises(ValueError):
        _num(b._q("C1:VDIV?"))                        # a shifted reply
    b.close()


def test_open_drains_a_queue_left_out_of_step(fake_visa, monkeypatch):
    """A crashed client left 12 bytes queued; clear() fails on this scope
    (VI_ERROR_SYSTEM_ERROR), so open() reads them away -- and the first query
    gets its own answer."""
    from fake_visa import FakeSDS
    monkeypatch.setattr(FakeSDS, "stale", b"\x05\x0a\x41\x0a junk\x0a\n")
    b = SiglentSDS(RES)
    b.open()
    inst = fake_visa[0]
    assert b.idn().startswith("Siglent")
    assert inst.clear_calls == 0 and inst.timeout == b._timeout_ms
    assert b.read_settings()["unread"] == []
    b.close()


def test_si_prefixes_and_units_are_scaled():
    """BUGS 2 and 3: "SARA 500.0KSa" is 500 kSa/s, "TRDL 12.0us" 12 us."""
    assert _num("SARA 500.0KSa") == 500e3
    assert _num("SARA 1.00GSa") == 1e9
    assert _num("SARA 1.00E+05Sa/s") == 1e5
    assert _num("TRDL 0.00us", time_unit=True) == 0.0
    assert _num("TRDL 12.0us", time_unit=True) == pytest.approx(12e-6)
    assert _num("TDIV 1.00E-03s", time_unit=True) == 1e-3
    assert _num("TDIV 1.00MS", time_unit=True) == pytest.approx(1e-3)   # SCPI: MS = milli
    assert _num("C1:OFST 3.60E-01V") == 0.36
    assert _num("X 500mV") == 0.5
    assert _num("SANU 8000") == 8000


def test_time_axis_uses_the_scaled_sample_rate(fake_visa):
    b = SiglentSDS(RES)
    b.open()
    inst = fake_visa[0]
    inst.st["SARA"], inst.st["SANU"], inst.st["TRDL"] = 500e3, 8000, 0.0
    t, v = b.read_traces(["ch1"], max_points=8000)
    # 8000 points at 500 kSa/s: 16 ms, centred on the trigger (was 1000x off)
    assert t[1] - t[0] == pytest.approx(2e-6)
    assert t[0] == pytest.approx(-8e-3)
    b.close()
