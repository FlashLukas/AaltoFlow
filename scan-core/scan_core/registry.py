"""
registry.py — the single source of "what can I sweep / measure".

Every knob is a Parameter with a stable `id`. Settables can be set (blocking
until settled) and read back; gettables can be read. A Registry is just a
namespace of them. The Scan Builder populates its dropdowns from here and the
engine drives/reads through here — so adding an instrument = adding Parameters,
and it appears everywhere with no other edits. That is the expandability story.

For real hardware, a Settable/Gettable wraps one of your ZeroMQ clients (the
~40-line adapters): `set` = client.set_x + wait-until-settled, `get` = read the
status/detector. Here we ship a SimRegistry with a toy MOKE/FMR physics so scans
show a real resonance line and a spatial spot — no hardware needed.
"""

from __future__ import annotations

import time

import numpy as np


class Parameter:
    def __init__(self, id: str, label: str, unit: str, kind: str):
        self.id = id
        self.label = label
        self.unit = unit
        self.kind = kind          # "settable" | "gettable"


class Settable(Parameter):
    def __init__(self, id, label, unit, limits, set_fn, get_fn):
        super().__init__(id, label, unit, "settable")
        self.limits = tuple(limits)     # (min, max) in `unit`
        self._set, self._get = set_fn, get_fn

    def set(self, value: float):
        lo, hi = self.limits
        value = max(lo, min(hi, float(value)))   # clamp: the safety envelope lives here
        self._set(value)                          # (real adapter blocks until settled)
        return value

    def get(self) -> float:
        return self._get()


class AxisSpec:
    """One inner axis that a detector brings along with its data.

    A VNA is the motivating case. It does not return a number per scan point --
    it returns a whole trace, because the frequency sweep happens IN HARDWARE,
    on the instrument, far faster than the engine could ever step it. That
    frequency axis is a real dimension of the measurement; it is simply swept by
    the instrument rather than by the odometer.

    `values_fn` is called ONCE at the start of a scan, not per point: reading
    1601 frequencies over ZeroMQ at every grid point would dominate the run.
    """

    def __init__(self, name: str, label: str = "", unit: str = "",
                 values_fn=None, length: int | None = None):
        self.name = name
        self.label = label or name
        self.unit = unit
        self.length = length
        self._values = values_fn

    def values(self):
        """The coordinate array for this axis (numpy)."""
        if self._values is None:
            n = self.length or 0
            return np.arange(n, dtype=float)
        return np.asarray(self._values())


class AcquireSpec:
    """How to make a slow detector actually take a fresh measurement.

    Reading a fast detector is one call: an NI analog input hands back a sample
    in microseconds. A VNA does not work that way. A sweep takes real time --
    tens of milliseconds to several seconds -- and the sequence is
    trigger, wait, read.

    Skipping the wait does NOT raise an error. It hands you whatever trace is
    still in the instrument's buffer, which is the PREVIOUS sweep, taken at the
    previous field. The map comes out one step behind and looks perfectly
    clean. It is the same failure as reading a stale `field_stable`, on the
    detector side.

    `group` is what stops s11, s21, s12 and s22 costing four sweeps: detectors
    sharing a group name are triggered ONCE together, waited on together, and
    then all read from the same acquisition.
    """

    def __init__(self, group: str, trigger_fn=None, wait_fn=None):
        self.group = group
        self._trigger = trigger_fn
        self._wait = wait_fn

    def trigger(self):
        if self._trigger:
            self._trigger()

    def wait(self):
        if self._wait:
            self._wait()


class Gettable(Parameter):
    """A detector.

    Scalar by default. Pass `axes` to declare that every read returns an ARRAY
    with its own inner dimensions -- the dataset then gains those dimensions
    after the scan's own, and several detectors sharing an axis name share one
    coordinate (s11, s21, s12 and s22 all hang off the same vna_freq).

    `dtype` may be "float", "int" or "complex". Complex is carried natively in
    memory and SPLIT into <id>_real and <id>_imag when the dataset is built, so
    the file stays conforming netCDF-4 and every other tool in the lab can read
    it. `scan_core.data.as_complex(ds, "s21")` puts it back together.
    """

    def __init__(self, id, label, unit, get_fn, axes=None, dtype="float",
                 acquire=None):
        super().__init__(id, label, unit, "gettable")
        self._get = get_fn
        self.axes = list(axes or [])          # [AxisSpec, ...]; empty = scalar
        self.dtype = dtype
        #: AcquireSpec for detectors that must be triggered and waited on.
        #: None = a plain read is already fresh (an NI sample, a status field).
        self.acquire = acquire

    @property
    def is_array(self) -> bool:
        return bool(self.axes)

    def get(self):
        return self._get()


class Action:
    """Something an instrument DOES, that a scan may ask for and wait on.

    "Take a VNA reference", "clear the reference". Not a Parameter, on purpose:
    a Parameter is a value you can sweep or record, and an action is neither --
    there is no coordinate array to build from "take a reference" and nothing to
    read back. Keeping actions in their own list means `settables()` and
    `gettables()` stay exactly what the axis palette and the detector list need,
    and nothing that iterates them has to learn to skip buttons.

    `run()` BLOCKS until the action has finished, exactly like `Settable.set`
    blocks until the knob has arrived. That is the whole contract a routine
    relies on: when `run()` returns, the next step may start.
    """

    kind = "action"

    def __init__(self, id: str, label: str, run_fn, help: str = ""):
        self.id = id
        self.label = label
        self.help = help
        self._run = run_fn

    def run(self):
        return self._run()


class Registry:
    def __init__(self):
        self._params: dict[str, Parameter] = {}
        # A separate dict, not a third `kind` in _params: `get(pid)` is what
        # validation and the engine use to find a settable or a detector, and an
        # action must never come back from it as if it were one.
        self._actions: dict[str, Action] = {}

    def add(self, p: Parameter) -> Parameter:
        self._params[p.id] = p
        return p

    def get(self, pid: str) -> Parameter | None:
        return self._params.get(pid)

    def settables(self) -> list[Settable]:
        return [p for p in self._params.values() if p.kind == "settable"]

    def gettables(self) -> list[Gettable]:
        return [p for p in self._params.values() if p.kind == "gettable"]

    # ---- actions (routines before/after a scan) --------------------------

    def add_action(self, a: Action) -> Action:
        self._actions[a.id] = a
        return a

    def actions(self) -> list[Action]:
        return list(self._actions.values())

    def get_action(self, aid: str) -> Action | None:
        return self._actions.get(aid)


# ───────────────────────────── simulated system ───────────────────────────────

#: The patterned sample the simulator pretends to be measuring: discs and bars
#: of permalloy on a substrate, the kind of thing that is actually under the
#: objective. Each island is (x_um, y_um, radius_um, aspect, df_MHz, amp).
#:
#: `df_MHz` is what makes a spatial map worth looking at: an island's shape
#: anisotropy shifts its resonance, so at a GIVEN frequency only some islands
#: are resonant and the rest are dark. Sweep the frequency and they light up in
#: turn -- which is exactly what an FMR map of a patterned array looks like, and
#: exactly what the Data tab's frequency slider is for. A single Gaussian blob
#: (what this used to be) shows none of that.
ISLANDS = (
    #  x     y    r   aspect   df    amp
    (-26.0,  22.0, 11.0, 1.00, -430.0, 1.00),   # big disc, lowest resonance
    (  2.0,  27.0,  8.0, 1.00, -150.0, 0.92),
    ( 28.0,  20.0,  6.0, 1.00,  180.0, 0.80),   # small disc, higher resonance
    (-30.0, -6.0,  14.0, 0.45,  -60.0, 0.85),   # bar, long axis along x
    (  0.0,  0.0,   9.0, 1.00,   40.0, 1.00),   # the one under the crosshair
    ( 27.0, -8.0,   7.0, 2.20,  330.0, 0.75),   # bar, long axis along y
    (-22.0, -30.0,  9.5, 1.00,  460.0, 0.70),
    (  4.0, -26.0,  6.5, 1.00,  620.0, 0.66),   # smallest, highest resonance
    ( 30.0, -30.0,  8.0, 0.60,  240.0, 0.78),
)

#: How much signal comes from the unpatterned film between the islands. Not
#: zero: a real sample has a continuous layer, and a map that is exactly zero
#: between the islands looks synthetic. It also keeps a resonance measurable
#: anywhere, so a field sweep at an arbitrary position still means something.
FILM_AMP = 0.18


class SimState:
    """Toy MOKE/FMR-ish physics so N-D scans produce recognisable structure.

    - A field-dependent resonance line f_res(B) (Kittel-like sqrt law), so a
      field × frequency map shows a curved bright line.
    - A PATTERNED SAMPLE in (x, y): discs and bars (`ISLANDS`), each with its
      own resonance offset, on a weakly responding film. A spatial map is then
      a picture of the pattern that CHANGES with frequency and field, instead of
      one blob that only gets brighter.
    - A Lorentzian lock-in response around the local resonance; amplitude scales
      with RF power and the antenna's field; device voltage nudges f_res.
    """
    def __init__(self):
        self.field_mT = 0.0
        self.rf_freq_MHz = 1000.0
        self.rf_power_dBm = -10.0
        self.rf_phase_deg = 0.0
        self.device_V = 0.0
        self.x_um = 0.0
        self.y_um = 0.0
        self.z_um = 0.0
        self._rng = np.random.default_rng(0)
        self._vna_freqs = np.linspace(500e6, 6.0e9, 401)
        self._vna_buffer = None
        self._vna_ref = None          # the stored reference trace (take_vna_reference)

    def _pattern(self) -> tuple[float, float]:
        """(coverage, resonance shift in MHz) at the current (x, y).

        Coverage uses a SUPER-Gaussian, exp(-(r/R)^6): flat on top with a fast
        edge, so an island reads as a patterned element with a definite rim
        rather than a smudge. Overlaps are handled by taking the strongest
        island, not by adding, because two patches of metal do not stack.

        The shift is a STEP where the island takes over (coverage above a half),
        not a fraction of the coverage. Sliding the resonance continuously from
        the film's value to the island's across the edge makes every off-resonant
        island wear a bright RING -- the rim passing through the drive frequency
        on its way. It is a pretty artefact and it is not what a patterned sample
        does: the element resonates as a whole, and its edge is a boundary, not a
        gradient of materials.
        """
        best_w = 0.0
        best_df = 0.0
        for x0, y0, r, aspect, df, amp in ISLANDS:
            dx = (self.x_um - x0) / r
            dy = (self.y_um - y0) / (r * aspect)
            rr = dx * dx + dy * dy
            if rr > 9.0:                      # far outside: skip the exp
                continue
            w = amp * float(np.exp(-(rr ** 3)))
            if w > best_w:
                best_w, best_df = w, df
        # Inside or outside, with nothing in between: an element's own response
        # AND its own resonance, or the bare film's. Letting the rim keep the
        # island's amplitude but the film's frequency lights a halo around every
        # island at the film line, which is the same artefact from the other
        # side.
        on = best_w >= 0.5
        return (best_w if on else 0.0), (best_df if on else 0.0)

    def _f_res(self) -> float:
        """The resonance HERE: the film's Kittel line on the bare substrate, the
        island's own line wherever an island is."""
        B = abs(self.field_mT)
        film = 500.0 + 3.0 * np.sqrt(B * (B + 300.0)) + 25.0 * self.device_V  # MHz
        return film + self._pattern()[1]

    def _amp(self) -> float:
        power_lin = 10.0 ** (self.rf_power_dBm / 20.0)
        w, _ = self._pattern()
        # The antenna's RF field falls off across the sample, so islands far
        # from it answer more weakly even on resonance -- a gradient across the
        # image, which is what stops it looking like a drawing.
        antenna = 0.75 + 0.25 * np.exp(-((self.y_um - 15.0) ** 2) / (2 * 55.0 ** 2))
        return power_lin * antenna * (FILM_AMP + w)

    def _linewidth_MHz(self) -> float:
        """Islands ring more sharply than the film: confinement quantises the
        modes, so the damping you see is lower and the peaks are narrower."""
        w, _ = self._pattern()
        return 60.0 - 28.0 * min(w, 1.0)

    def trigger_vna(self):
        """Start a sweep and latch the result into the buffer.

        A real VNA returns from the trigger BEFORE the sweep finishes, and you
        poll it for completion; the sim latches immediately because there is
        nothing to wait for. What it faithfully reproduces is the important
        part: `read_vna` returns whatever is in the buffer, so a scan that
        forgets to trigger records the PREVIOUS point's trace and never
        complains.
        """
        self._vna_buffer = self.vna_trace(self._vna_freqs)

    def read_vna(self):
        """Whatever is in the buffer -- stale if nothing triggered a sweep."""
        if self._vna_buffer is None:
            self._vna_buffer = self.vna_trace(self._vna_freqs)
        return self._vna_buffer

    def take_vna_reference(self):
        """Sweep NOW and keep that trace as the reference.

        VNA-FMR is normally read as u = (S - S_ref) / S_ref: dividing by a trace
        taken where the sample is far off resonance (a large field) removes the
        cables, the waveguide and the frequency-dependent loss, and leaves the
        magnetic response. A fresh sweep, not the buffer: the buffer belongs to
        whatever point was measured last, which is not where the reference is.
        """
        self._vna_ref = self.vna_trace(self._vna_freqs)

    def autofocus(self):
        """A stand-in for the camera's focus search: takes a moment, and
        counts itself, so a THROUGHOUT routine can be tried without a lab."""
        time.sleep(0.05)
        self.n_autofocus = getattr(self, "n_autofocus", 0) + 1

    def read_vna_u(self):
        """u = (S - S_ref) / S_ref of the buffered sweep; NaN with no reference.

        NaN rather than an error: a scan that forgot its reference still records
        S itself, and a column of NaN says plainly which quantity is missing.
        """
        trace = self.read_vna()
        if self._vna_ref is None:
            return np.full(trace.shape, np.nan + 1j * np.nan, dtype=np.complex128)
        return (trace - self._vna_ref) / self._vna_ref

    def read_vna_ln(self):
        """ln(S / S_ref) of the buffered sweep; NaN with no reference.

        A film on a transmission line gives S = A exp(i eta chi), so the
        LOGARITHM is what is proportional to the susceptibility -- at any line
        depth, where u is only its small-signal limit (u ~ ln(1 + u)). No
        prefactor: eta belongs to the waveguide and the sample.
        """
        trace = self.read_vna()
        if self._vna_ref is None:
            return np.full(trace.shape, np.nan + 1j * np.nan, dtype=np.complex128)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.log(trace / self._vna_ref)

    def vna_trace(self, freqs_Hz):
        """A VNA-FMR style complex S21 trace over `freqs_Hz`.

        One scan point, one whole trace: this is what a real VNA gives you,
        because the frequency sweep happens in the instrument. A complex
        Lorentzian absorption dip sits at the field-dependent resonance, so a
        field sweep produces the curved resonance line you would actually
        measure -- and the phase behaves like a real resonance too, which a
        magnitude-only simulator would hide.
        """
        f = np.asarray(freqs_Hz, dtype=float)
        f_res = self._f_res() * 1e6                      # MHz -> Hz
        linewidth = self._linewidth_MHz() * 1e6          # Hz (HWHM)
        amp = 0.6 * self._amp()
        # Standard notch/absorption form: 1 - A / (1 + 2i(f - f0)/G).
        # At f = f0 this is 1 - A, a genuine MINIMUM of |S21| exactly on
        # resonance, with the dispersive phase roll either side.
        # (The obvious-looking A(G/2)/((f-f0) + iG/2) is wrong for this: it
        # PEAKS at f0 and its |S21| minimum sits tens of MHz off, which quietly
        # breaks any test that locates the resonance by argmin.)
        s21 = 1.0 - amp / (1.0 + 2j * (f - f_res) / linewidth)
        noise = 0.002 * (self._rng.standard_normal(f.size)
                         + 1j * self._rng.standard_normal(f.size))
        return (s21 + noise).astype(np.complex128)

    def lockin(self):
        width = self._linewidth_MHz()                  # MHz linewidth
        detune = (self.rf_freq_MHz - self._f_res()) / width
        lorentz = 1.0 / (1.0 + detune ** 2)            # absorptive
        disp = detune / (1.0 + detune ** 2)            # dispersive
        r = self._amp() * lorentz
        noise = 0.01 * self._rng.standard_normal()
        phi = np.deg2rad(self.rf_phase_deg)
        x = r * np.cos(phi) - 0.3 * disp * np.sin(phi) + noise
        y = r * np.sin(phi) + 0.3 * disp * np.cos(phi) + noise
        R = float(np.hypot(x, y))
        return dict(x=float(x), y=float(y), R=R,
                    phi=float(np.rad2deg(np.arctan2(y, x))),
                    aux=float(0.5 + 0.05 * self._rng.standard_normal()))


def build_sim_registry() -> Registry:
    """A ready-to-scan simulated instrument suite."""
    s = SimState()
    reg = Registry()

    def settable(id, label, unit, limits, attr):
        reg.add(Settable(id, label, unit, limits,
                         set_fn=lambda v, a=attr: setattr(s, a, v),
                         get_fn=lambda a=attr: getattr(s, a)))

    settable("field",     "Magnetic field", "mT",  (-200, 200), "field_mT")
    settable("rf_freq",   "RF frequency",   "MHz", (10, 6000),  "rf_freq_MHz")
    settable("rf_power",  "RF power",       "dBm", (-30, 15),   "rf_power_dBm")
    settable("rf_phase",  "RF phase",       "deg", (-180, 180), "rf_phase_deg")
    settable("device_v",  "Device voltage", "V",   (-10, 10),   "device_V")
    settable("pos_x",     "Position X",     "um",  (-100, 100), "x_um")
    settable("pos_y",     "Position Y",     "um",  (-100, 100), "y_um")
    settable("pos_z",     "Position Z",     "um",  (-50, 50),   "z_um")

    reg.add(Gettable("lockin_r",   "Lock-in R",   "V",   lambda: s.lockin()["R"]))
    reg.add(Gettable("lockin_x",   "Lock-in X",   "V",   lambda: s.lockin()["x"]))
    reg.add(Gettable("lockin_y",   "Lock-in Y",   "V",   lambda: s.lockin()["y"]))
    reg.add(Gettable("lockin_phi", "Lock-in phase", "deg", lambda: s.lockin()["phi"]))
    reg.add(Gettable("aux_in",     "Aux in",      "V",   lambda: s.lockin()["aux"]))

    # An ARRAY detector, the VNA case: one read returns a whole trace, because
    # the frequency sweep happens in the instrument rather than in the odometer.
    # The scan gains a `vna_freq` dimension after its own, and any other
    # detector declaring the same axis name shares this one coordinate.
    freq_axis = AxisSpec("vna_freq", "Frequency", "Hz",
                         values_fn=lambda: s._vna_freqs)
    # `acquire` is what makes the read FRESH. Reading a VNA cold hands back the
    # previous sweep -- taken at the previous point -- and nothing raises.
    vna_sweep = AcquireSpec("vna", trigger_fn=s.trigger_vna)
    reg.add(Gettable("s21", "S21", "", s.read_vna,
                     axes=[freq_axis], dtype="complex", acquire=vna_sweep))
    # u off the SAME sweep: same group, so recording s21 and u together costs
    # one acquisition, and u is computed from exactly the trace s21 stores.
    reg.add(Gettable("u", "Permeability u = (S21 - ref)/ref", "", s.read_vna_u,
                     axes=[freq_axis], dtype="complex", acquire=vna_sweep))
    reg.add(Gettable("ln_ratio", "ln(S21 / ref)", "", s.read_vna_ln,
                     axes=[freq_axis], dtype="complex", acquire=vna_sweep))

    # An ACTION, so the before/after-scan routines can be tried without a lab:
    # "go to a far-off field, take a reference, then sweep" runs end to end here.
    reg.add_action(Action("vna_reference", "Take VNA reference",
                          s.take_vna_reference,
                          help="Sweep now and store the trace as the reference "
                               "that u = (S21 - ref)/ref divides by."))

    reg.add_action(Action("sim_autofocus", "Autofocus (simulated)", s.autofocus,
                          help="Stands in for camera.autofocus: waits 50 ms. For "
                               "trying a THROUGHOUT routine without the rig."))

    reg._state = s     # handy for tests
    return reg
