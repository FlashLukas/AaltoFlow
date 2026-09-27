"""The real GPIB backend against a FAKE VISA instrument (no pyvisa, no bus).

It pins down what we took from the manual -- the statements sent, the reply
parsing, and the rule for deciding that a move has finished -- so that when the
lab instrument disagrees, the difference is found in one place."""

from cs260.backends.cornerstone import CornerstoneGPIB


class FakeInst:
    """Answers queries like the manual says a Cornerstone does over GPIB."""

    def __init__(self):
        self.timeout = 0
        self.written = []
        self.wave = 500.0
        self.grat = "1,1200,VIS"
        self.shutter = "C"
        self.filter = "1"
        self.stb = "00"
        self.err = "0"
        self.path = []            # scripted WAVE? replies during a move

    def write(self, cmd):
        self.written.append(cmd)

    def query(self, cmd):
        self.written.append(cmd)
        if cmd == "WAVE?":
            if self.path:
                self.wave = self.path.pop(0)
            return f"{self.wave:.3f}\r"
        if cmd == "GRAT?":
            return self.grat + "\r"
        if cmd == "SHUTTER?":
            return self.shutter
        if cmd == "FILTER?":
            return self.filter
        if cmd == "OUTPORT?":
            return "1"
        if cmd == "STEP?":
            return "123456"
        if cmd == "STB?":
            s, self.stb = self.stb, "00"
            return s
        if cmd == "ERROR?":
            return self.err
        if cmd == "INFO?":
            return "Oriel,Model 74100 Cornerstone 260,SN0,V1"
        if cmd == "GRAT1LINES?":
            return "1200"
        if cmd == "GRAT1LABEL?":
            return "VIS"
        return ""


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def _backend(**kw):
    clock = Clock()
    b = CornerstoneGPIB(clock=clock, **kw)
    b._inst = FakeInst()                     # skip open(): no pyvisa here
    return b, b._inst, clock


def test_statements_are_the_manuals():
    b, inst, _ = _backend(filter_wheel=True, dual_port=True)
    b.goto(632.8); b.set_grating(2); b.set_filter(3); b.set_port(2)
    b.set_shutter(True); b.set_shutter(False); b.step(-5); b.abort(); b.calibrate(546.074)
    assert inst.written == ["GOWAVE 632.800", "GRAT 2", "FILTER 3", "OUTPORT 2",
                            "SHUTTER O", "SHUTTER C", "STEP -5", "ABORT",
                            "CALIBRATE 546.074"]


def test_read_state_parses_replies():
    b, inst, _ = _backend(filter_wheel=True)
    inst.shutter = "O"
    st = b.read_state()
    assert st.wavelength_nm == 500.0 and st.grating == 1 and st.shutter_open
    assert st.filter == 1 and st.step_position == 123456
    assert st.moving is False and st.error_code is None
    assert b.grating_info(1) == (1200, "VIS")


def test_move_is_done_only_at_the_target_and_stable():
    b, inst, _ = _backend()
    b.goto(700.0)
    inst.path = [500.0, 610.0, 699.9, 699.9]
    assert b.read_state().moving           # still at the old value
    assert b.read_state().moving           # travelling
    assert b.read_state().moving           # at target, but not yet confirmed
    assert b.read_state().moving is False  # at target twice: arrived


def test_error_ends_the_pending_move_and_is_reported():
    b, inst, _ = _backend()
    b.goto(5000.0)
    inst.stb, inst.err = "32", "3"
    st = b.read_state()
    assert st.error_code == 3 and st.moving is False


def test_grating_swap_waits_a_minimum_time():
    b, inst, clock = _backend()
    b.set_grating(2)
    inst.grat = "2,600,NIR"
    inst.wave = 0.0
    assert b.read_state().moving
    assert b.read_state().moving           # stable, but < 1 s since the command
    clock.t = 2.0
    assert b.read_state().moving is False


def test_status_byte_in_hex_is_still_an_error():
    """Manual 16.6 says STB? answers "32" on an error, 16.7 shows "20" (the
    same bit 5, written in hex). Any non-zero byte must count."""
    b, inst, _ = _backend()
    b.goto(5000.0)
    inst.stb, inst.err = "20", "3"
    st = b.read_state()
    assert st.error_code == 3 and st.moving is False


def test_filter_wheel_in_transit_is_not_an_error():
    """FILTER? answers 0 AND sets an error while the wheel is between
    positions (manual 16.6). During OUR filter move that is just 'moving'."""
    b, inst, _ = _backend(filter_wheel=True)
    b.set_filter(3)
    inst.filter, inst.stb, inst.err = "0", "32", "6"
    st = b.read_state()
    assert st.moving and st.error_code is None
    inst.filter = "3"
    assert b.read_state().moving is False


def test_filter_move_is_polled_even_if_not_configured():
    """A wheel switched on by set_config after the backend was built: its
    move must still be seen to finish, and configure_accessories arms it."""
    b, inst, _ = _backend()
    b.set_filter(2)
    inst.filter = "2"
    assert b.read_state().moving is False
    inst.written.clear()
    b.read_state()
    assert "FILTER?" not in inst.written      # idle + not fitted: not asked
    b.configure_accessories(True, False)
    b.read_state()
    assert "FILTER?" in inst.written
