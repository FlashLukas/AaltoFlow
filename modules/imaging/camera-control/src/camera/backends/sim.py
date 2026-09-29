"""Simulated camera + stage + focus -- a CLOSED-LOOP scene (blueprint §3).

This is what makes the whole feedback system testable with no hardware.  The
three simulators share state so the rendered image RESPONDS to motion:

  * The laser spot is FIXED in the image (it is the reference).
  * The sample -- and the template pattern printed on it -- moves with the XY
    stage.  So a stage move shifts the template (and the scanning array pinned to
    it) across the frame, exactly as on the microscope.
  * Focus: the spot grows (and the image blurs) as Z departs from the true best
    focus voltage, so both spot-area and edge/FFT autofocus have something to
    optimise.

Move the stage -> the template shifts -> the stabiliser measures the new
spot->point distance and moves the stage to null it -> it converges.  Sweep Z ->
the spot area bottoms out at true focus.  All offline, all deterministic.

Sign convention (must match the stabiliser in camera.py):
    scene_shift_px = (stage_um - stage_ref_um) / pixel_size_um
    template_px    = template_home_px + scene_shift_px
i.e. a POSITIVE stage step moves features in the +pixel direction.
"""

from __future__ import annotations

import math
import threading

import cv2
import numpy as np

from .base import DirectionalCounter


def _F(display, ftype, value=None, vmin=None, vmax=None, inc=None, unit="",
       options=None, writable=True, cat=""):
    """Build a camera-feature descriptor (see CameraBackend.features())."""
    return {"display": display, "type": ftype, "value": value, "min": vmin,
            "max": vmax, "inc": inc, "unit": unit, "options": options,
            "writable": writable, "category": cat}


# --------------------------------------------------------------------------- #
# XY stage simulator
# --------------------------------------------------------------------------- #
class SimXYStage:
    """A fast, near-instant XY stage (a piezo settles in ms).

    ``slew`` is the fraction of the remaining distance covered per ``read_xy``
    (1.0 = teleport, the default -- deterministic and fine for tests; the GUI can
    set <1 for visibly smooth motion).
    """

    def __init__(self, x0: float = 65.0, y0: float = 65.0, slew: float = 1.0):
        self._x = float(x0)
        self._y = float(y0)
        self._tx = float(x0)
        self._ty = float(y0)
        self._slew = float(slew)
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def move_xy(self, x_um: float, y_um: float) -> None:
        self._tx = float(np.clip(x_um, -1000.0, 1000.0))
        self._ty = float(np.clip(y_um, -1000.0, 1000.0))

    def read_xy(self) -> tuple:
        self._x += (self._tx - self._x) * self._slew
        self._y += (self._ty - self._y) * self._slew
        return (self._x, self._y)

    def moving(self) -> bool:
        return abs(self._tx - self._x) > 1e-4 or abs(self._ty - self._y) > 1e-4


# --------------------------------------------------------------------------- #
# Z focus simulator
# --------------------------------------------------------------------------- #
class SimZFocus:
    """A Z piezo whose true best focus sits at ``z_focus`` volts."""

    def __init__(self, z0: float = 7.6, z_focus: float = 7.6,
                 vmin: float = 0.0, vmax: float = 75.0):
        self._v = float(z0)
        self.z_focus = float(z_focus)
        self._vmin = float(vmin)
        self._vmax = float(vmax)
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def set_z(self, volts: float) -> None:
        self._v = float(np.clip(volts, self._vmin, self._vmax))

    def read_z(self) -> float:
        return self._v

    def z_range(self) -> tuple:
        return (self._vmin, self._vmax)


class SimSlipStickZ(SimZFocus):
    """An open-loop inertia Z (KIM101 + PIA25) whose steps are not the same size
    up as down.

    ``read_z()`` is the step COUNTER (what kim reports: commanded steps x a
    nominal step size); ``true_z()`` is where the sample really is, and that is
    what the simulated image follows. A move up travels ``up_gain`` x the
    commanded distance, a move down ``down_gain`` x, so every reversal makes the
    counter and the truth drift apart -- the reason the symmetric sweep parks
    off focus on the real rig, and the reason the one-way routine exists.
    """

    open_loop = True

    def __init__(self, z0: float = 0.0, z_focus: float = 7.6, vmin: float = -100.0,
                 vmax: float = 100.0, up_gain: float = 1.0, down_gain: float = 0.7,
                 nominal_step: float = 1.0):
        super().__init__(z0=z0, z_focus=z_focus, vmin=vmin, vmax=vmax)
        self._true = float(z0)
        self.up_gain = float(up_gain)
        self.down_gain = float(down_gain)
        # Like kim: the counter is in STEPS, and the reading is steps x one
        # nominal step size (here 1 Z unit per "step", fractional steps
        # allowed). After a Z step calibration (set_step_sizes) read_z / set_z
        # use the two measured sizes instead -- see DirectionalCounter.
        self.nominal_step = float(nominal_step)
        self._sizes: tuple | None = None
        self._dc = DirectionalCounter()

    def _move_raw(self, counter_units: float) -> None:
        """Move the counter to this reading (steps x nominal); the SAMPLE moves
        up_gain or down_gain times as far -- the slip-stick asymmetry."""
        new = float(np.clip(counter_units, self._vmin, self._vmax))
        delta = new - self._v
        self._true += delta * (self.up_gain if delta > 0 else self.down_gain)
        self._v = new

    def read_z(self) -> float:
        if self._sizes is None:
            return self._v
        up, down = self._sizes
        return self._dc.position(self.counter_steps(), up, down, self.nominal_step)

    def set_z(self, volts: float) -> None:
        if self._sizes is None:
            self._move_raw(volts)
            return
        up, down = self._sizes
        steps = self._dc.plan(self.counter_steps(), volts, up, down, self.nominal_step)
        self._move_raw(steps * self.nominal_step)

    # -- the step counter, raw (what the Z step calibration walks with) -------
    def counter_steps(self) -> float:
        return self._v / self.nominal_step

    def move_counter(self, steps: float) -> None:
        self._move_raw(float(steps) * self.nominal_step)

    def step_sizes(self) -> tuple:
        """(up, down) Z units per step as the stage believes them (nominal until set)."""
        return self._sizes if self._sizes is not None else (self.nominal_step,
                                                            self.nominal_step)

    def set_step_sizes(self, up: float, down: float) -> None:
        self._sizes = (float(up), float(down))
        self._dc.reset()

    def true_z(self) -> float:
        return self._true


# --------------------------------------------------------------------------- #
# A COHERENT laser spot through focus (2026-09-28)
# --------------------------------------------------------------------------- #
class CoherentSpot:
    """A coherent laser spot whose defocused image has rings and a central HOLE.

    Why this model (Lukas, 2026-09-28: "the laser is highly coherent, so a
    defocused spot has RINGS and can have a HOLE in the centre"): the field is
    a coherent sum of TWO modes of one Gaussian beam -- the fundamental LG00
    and the first radial Laguerre-Gauss mode LG10 (a centre with one ring):

        E(r, z) ~ (1/w) exp(-r^2/w^2) [a0 + a1 e^{i(2 psi + phi)} (1 - 2 r^2/w^2)]

    with w(z) = w0 sqrt(1 + zeta^2), zeta = z / zR, and psi = arctan(zeta) the
    Gouy phase. The ring mode picks up TWICE the Gouy phase of the fundamental
    (2p+1 with p = 1 vs 0), so the two slide in and out of phase through focus:
    where 2 psi + phi = 0 the centre is bright (a tight-looking spot), where
    2 psi + phi = pi and |a0| = |a1| the centre is exactly DARK -- a donut.
    (The wavefront curvature exp(-i k r^2 / 2R) is common to both modes and
    drops out of the intensity.) Any real aberrated or truncated coherent beam
    is such a mode sum, with more modes; two are the smallest case with a hole.

    The property the autofocus relies on: for ANY paraxial coherent field the
    second moment is exactly a parabola in z. Here, analytically (orthonormal
    modes, |a0|^2 + |a1|^2 = 1, <r^2> of LG_p0 = w^2 (2p+1)/2, cross term
    -w^2/2):
        <r^2>(z) = w(z)^2 [A + B cos(2 psi + phi)],  A = (|a0|^2 + 3|a1|^2)/2,
                                                     B = -|a0||a1|
    and w^2 cos(2 psi + phi) = w0^2 [(1 - zeta^2) cos phi - 2 zeta sin phi],
    a quadratic in zeta. Its vertex is shifted from the geometric waist, so the
    model offsets zeta such that the SECOND-MOMENT waist sits at defocus 0:
    "true focus" in this simulator = the smallest sigma^2. (The plane where
    the centre is brightest is elsewhere -- for the defaults (phi = 3 pi / 4)
    1.9 zR before it, and the HOLE 0.96 zR after it. For a non-Gaussian beam
    those planes differ; the second moment is the one that is a clean
    parabola, and what the D4sigma autofocus finds.)

    Rendered at pixel centres, energy conserving (a defocused spot dims),
    scaled so the brightest point over all z is ``peak`` counts.
    """

    def __init__(self, w0_px=10.0, zr=3.0, mix=1.0, phase=0.75 * math.pi, peak=220.0):
        self.w0 = float(w0_px)
        self.zr = max(1e-9, float(zr))
        n = math.sqrt(1.0 + float(mix) ** 2)
        self.a0, self.a1 = 1.0 / n, float(mix) / n
        self.phi = float(phase)
        self.A = 0.5 * (self.a0 ** 2 + 3.0 * self.a1 ** 2)
        self.B = -self.a0 * self.a1
        # <r^2> / w0^2 = qa zeta^2 + qb zeta + qc
        self.qa = self.A - self.B * math.cos(self.phi)
        self.qb = -2.0 * self.B * math.sin(self.phi)
        self.qc = self.A + self.B * math.cos(self.phi)
        self.zeta_star = -self.qb / (2.0 * self.qa)          # the sigma^2 waist
        # the brightest point over all z (on a grid) sets the counts scale
        best = 0.0
        rho2 = np.linspace(0.0, 3.0, 301)
        for zeta in np.linspace(-6.0, 6.0, 1201):
            best = max(best, float(self._shape(rho2, zeta).max()))
        self.scale = float(peak) / best

    def _zeta(self, defocus: float) -> float:
        return self.zeta_star + float(defocus) / self.zr

    def _shape(self, rho2, zeta):
        """Relative intensity at r^2 / w(z)^2 = rho2, including the 1/w^2 dimming."""
        psi = math.atan(zeta)
        f = self.a0 + self.a1 * np.exp(1j * (2.0 * psi + self.phi)) * (1.0 - 2.0 * rho2)
        return np.exp(-2.0 * rho2) * np.abs(f) ** 2 / (1.0 + zeta * zeta)

    def w(self, defocus: float) -> float:
        """The Gaussian beam's 1/e^2 radius at this defocus, px."""
        z = self._zeta(defocus)
        return self.w0 * math.sqrt(1.0 + z * z)

    def true_sigma2(self, defocus: float) -> float:
        """The exact per-axis second moment (px^2) at this defocus (= <r^2>/2)."""
        z = self._zeta(defocus)
        return 0.5 * self.w0 ** 2 * (self.qa * z * z + self.qb * z + self.qc)

    def hole_defocus(self) -> float:
        """Defocus where the centre is darkest (exactly dark when |a0| = |a1|)."""
        zeta = math.tan((math.pi - self.phi) / 2.0)
        return (zeta - self.zeta_star) * self.zr

    def intensity(self, xx, yy, cx, cy, defocus):
        z = self._zeta(defocus)
        w2 = self.w0 ** 2 * (1.0 + z * z)
        rho2 = ((xx - cx) ** 2 + (yy - cy) ** 2) / w2
        return self.scale * self._shape(rho2, z)

    def add_to(self, frame: np.ndarray, center, defocus: float) -> None:
        """Add the spot's light to a FLOAT ``frame`` in place (no clipping here)."""
        h, w = frame.shape
        r = int(math.ceil(4.0 * self.w(defocus))) + 2     # exp(-32): nothing beyond
        x0, x1 = max(0, int(center[0]) - r), min(w, int(center[0]) + r + 1)
        y0, y1 = max(0, int(center[1]) - r), min(h, int(center[1]) + r + 1)
        if x1 <= x0 or y1 <= y0:
            return
        yy, xx = np.ogrid[y0:y1, x0:x1]
        frame[y0:y1, x0:x1] += self.intensity(xx, yy, center[0], center[1], defocus)


# --------------------------------------------------------------------------- #
# Camera simulator (renders the scene from the stage + Z)
# --------------------------------------------------------------------------- #
def _make_glyph(size: int = 60) -> np.ndarray:
    """An asymmetric 'F'-like glyph so template matching has no ambiguity.

    Intensities are kept WELL BELOW the laser-spot threshold (~200) so the spot
    finder never mistakes the pattern for the spot -- the pattern is a mid-grey
    feature, the spot is the bright peak.  Grayscale correlation still locks onto
    the pattern regardless of its absolute brightness.
    """
    g = np.zeros((size, size), np.uint8)
    cv2.rectangle(g, (18, 10), (26, 50), 120, -1)   # vertical stem
    cv2.rectangle(g, (18, 10), (44, 18), 120, -1)   # top bar
    cv2.rectangle(g, (18, 26), (38, 34), 120, -1)   # middle bar
    cv2.circle(g, (40, 44), 4, 90, -1)              # asymmetric dot
    return g


class SimCamera:
    """Render a frame that depends on the shared SimXYStage + SimZFocus."""

    def __init__(
        self,
        stage: SimXYStage,
        zfocus: SimZFocus,
        pixel_size_x_um: float = 0.4130,
        pixel_size_y_um: float = 0.4130,
        width: int = 640,
        height: int = 480,
        spot_px: tuple = (320.0, 240.0),
        template_home_px: tuple = (440.0, 260.0),
        z_focus: float = 7.6,          # scene's true best-focus voltage
        base_sigma: float = 3.0,
        defocus_gain: float = 0.9,     # spot sigma growth per volt of defocus
        blur_gain: float = 0.25,       # image blur per volt of defocus
        noise: float = 3.0,
        seed: int = 1234,
        # The laser spot: "gaussian" (default: the old toy, a Gaussian whose
        # sigma grows linearly with defocus at full brightness) or "coherent"
        # (CoherentSpot: rings, a hole, energy conserved). Parameters of the
        # coherent one: waist radius, Rayleigh range (Z units), ring-mode
        # amplitude and phase, and the brightest it gets (> 255 saturates).
        spot_model: str = "gaussian",
        coherent_w0_px: float = 10.0,
        coherent_zr: float = 3.0,
        coherent_mix: float = 1.0,
        coherent_phase: float = 0.75 * math.pi,
        coherent_peak: float = 220.0,
        # 8 = only 8-bit frames (as always). 10/12/16 = every grab ALSO leaves
        # a full-depth copy of the same frame for last_deep(), like the lab
        # camera running in Mono12 -- quantised from the same noisy scene, so
        # the two differ only by the rounding to 8 bit.
        bit_depth: int = 8,
    ):
        self.stage = stage
        self.zfocus = zfocus
        self.px_x = float(pixel_size_x_um)
        self.px_y = float(pixel_size_y_um)
        self.w = int(width)
        self.h = int(height)
        self.spot_px = (float(spot_px[0]), float(spot_px[1]))
        self.template_home_px = (float(template_home_px[0]), float(template_home_px[1]))
        # The scene's true focus.  Prefer the Z backend's own z_focus if it has
        # one (SimZFocus does); otherwise the scene owns it -- so the SimCamera
        # works with ANY ZFocus, including a RemoteZFocus talking to the external
        # zpiezo service.
        self.z_focus = float(getattr(zfocus, "z_focus", z_focus))
        self.base_sigma = float(base_sigma)
        self.defocus_gain = float(defocus_gain)
        self.blur_gain = float(blur_gain)
        self.noise = float(noise)
        self._glyph = _make_glyph(60)
        self.spot_model = "coherent" if str(spot_model).lower() == "coherent" else "gaussian"
        self.coherent = CoherentSpot(coherent_w0_px, coherent_zr, coherent_mix,
                                     coherent_phase, coherent_peak)
        self._rng = np.random.default_rng(seed)
        self.bit_depth = int(bit_depth) if int(bit_depth) in (10, 12, 16) else 8
        self._deep = None                 # (uint16 frame, bits) of the last grab
        # Stage position at which the template sits at its home pixel.
        self._stage_ref = stage.read_xy()
        self._open = False

        # Simulated camera parameters (GenICam-style feature model).  Defaults
        # are the IDENTITY (exposure 5000/gain 1/gamma 1/black 0) so the rendered
        # image is unchanged out of the box; changing them visibly affects grab(),
        # which makes the GUI parameter panel demonstrable with no hardware.
        self._flock = threading.Lock()
        self._feat: dict = {
            "ExposureTime": _F("Exposure Time", "float", 5000.0, 20.0, 100000.0, 1.0, "us", cat="Acquisition"),
            "AcquisitionFrameRate": _F("Frame Rate", "float", 15.0, 1.0, 60.0, 0.5, "fps", cat="Acquisition"),
            "Gain": _F("Gain", "float", 1.0, 1.0, 16.0, 0.1, "", cat="Analog"),
            "GainAuto": _F("Gain Auto", "enum", "Off", options=["Off", "Continuous"], cat="Analog"),
            "Gamma": _F("Gamma", "float", 1.0, 0.3, 3.0, 0.05, "", cat="Analog"),
            "BlackLevel": _F("Black Level", "int", 0, 0, 64, 1, "", cat="Analog"),
            "PixelFormat": _F("Pixel Format", "enum", f"Mono{self.bit_depth}",
                              options=[f"Mono{self.bit_depth}"], cat="ImageFormat"),
            "DeviceModelName": _F("Model", "string", "SimCamera", writable=False, cat="Device"),
        }

    # -- backend interface ------------------------------------------------- #
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "SimCamera (synthetic closed-loop scene)"

    # -- camera-parameter feature model ------------------------------------ #
    def features(self) -> list:
        with self._flock:
            out = []
            for name, d in self._feat.items():
                item = dict(d)
                item["name"] = name
                out.append(item)
            return out

    def get_feature(self, name: str):
        with self._flock:
            d = self._feat.get(name)
            return None if d is None else d.get("value")

    def set_feature(self, name: str, value) -> None:
        with self._flock:
            d = self._feat.get(name)
            if d is None:
                raise KeyError(f"unknown feature {name!r}")
            if not d.get("writable", True):
                raise PermissionError(f"feature {name!r} is read-only")
            t = d["type"]
            if t in ("float", "int"):
                v = float(value) if t == "float" else int(round(float(value)))
                if d.get("min") is not None:
                    v = max(d["min"], v)
                if d.get("max") is not None:
                    v = min(d["max"], v)
                d["value"] = v
            elif t == "bool":
                d["value"] = bool(value)
            elif t == "enum":
                if str(value) not in (d.get("options") or []):
                    raise ValueError(f"{value!r} not a valid entry for {name!r}")
                d["value"] = str(value)
            elif t == "command":
                pass   # nothing to execute in the sim
            else:
                d["value"] = str(value)

    def _photometrics(self):
        with self._flock:
            return (self._feat["ExposureTime"]["value"] / 5000.0,
                    self._feat["Gain"]["value"],
                    float(self._feat["Gamma"]["value"]),
                    self._feat["BlackLevel"]["value"])

    # -- scene helpers ----------------------------------------------------- #
    def template_center_px(self) -> tuple:
        """Where the template currently sits (for tests / reference capture)."""
        sx, sy = self.stage.read_xy()
        shift_x = (sx - self._stage_ref[0]) / self.px_x
        shift_y = (sy - self._stage_ref[1]) / self.px_y
        return (self.template_home_px[0] + shift_x,
                self.template_home_px[1] + shift_y)

    def _paste_subpixel(self, frame: np.ndarray, stamp: np.ndarray,
                        cx: float, cy: float) -> None:
        """Composite ``stamp`` centred at float (cx, cy) via an affine warp."""
        sh, sw = stamp.shape
        tx = cx - sw / 2.0
        ty = cy - sh / 2.0
        m = np.array([[1, 0, tx], [0, 1, ty]], dtype=np.float32)
        warped = cv2.warpAffine(stamp, m, (frame.shape[1], frame.shape[0]),
                                flags=cv2.INTER_LINEAR)
        np.maximum(frame, warped, out=frame)

    def grab(self) -> np.ndarray:
        coherent = self.spot_model == "coherent"
        if coherent:
            # camera noise is added LAST (below): it is made in the sensor and
            # is not blurred by the defocus of the scene
            frame = np.full((self.h, self.w), 8, np.uint8)
        else:
            frame = (self._rng.normal(8, self.noise, (self.h, self.w))
                     .clip(0, 255).astype(np.uint8))

        # Template pattern moves with the stage.
        tcx, tcy = self.template_center_px()
        self._paste_subpixel(frame, self._glyph, tcx, tcy)

        # Defocus blur of the imaged scene (drives edge/FFT autofocus).
        # A slip-stick Z reports its step COUNTER; the scene follows where it
        # really is (true_z), which is the whole point of that simulator.
        z_now = getattr(self.zfocus, "true_z", self.zfocus.read_z)()
        dz = abs(z_now - self.z_focus)
        blur_sigma = self.blur_gain * dz
        if blur_sigma > 0.05:
            frame = cv2.GaussianBlur(frame, (0, 0), blur_sigma)

        self._deep = None
        if coherent:
            # light ADDS to the background (the moments rely on it), the power
            # is conserved (a defocused spot really gets dimmer), then the
            # sensor adds its noise and clips at 255 (saturation)
            f = frame.astype(np.float64)
            self.coherent.add_to(f, self.spot_px, z_now - self.z_focus)
            f += self._rng.normal(0.0, self.noise, f.shape)
            if self.bit_depth > 8:
                return self._quantise_both(f)
            # The exposure acts on the LIGHT, before the digitiser clips it
            # (2026-09-29): it used to scale the already-clipped 8-bit frame,
            # so a shorter exposure turned a saturated spot into a dimmer
            # FLAT TOP instead of an unsaturated spot -- the autofocus
            # exposure could not be tried in the simulator.
            expo, gain, gamma, black = self._photometrics()
            if expo != 1.0 or gain != 1.0 or black != 0:
                f = f * (expo * gain) + black
            if gamma != 1.0:
                f = 255.0 * np.clip(f / 255.0, 0.0, 1.0) ** (1.0 / gamma)
            return np.clip(np.rint(f), 0, 255).astype(np.uint8)
        else:
            # The laser spot: fixed position, sigma grows with defocus (area metric).
            sigma = self.base_sigma * (1.0 + self.defocus_gain * dz)
            yy, xx = np.ogrid[:self.h, :self.w]
            g = 255.0 * np.exp(-(((xx - self.spot_px[0]) ** 2 +
                                  (yy - self.spot_px[1]) ** 2) / (2 * sigma ** 2)))
            frame = np.maximum(frame, g.astype(np.uint8))

        # Apply the simulated camera parameters (exposure/gain/black level/gamma).
        # With the identity defaults this is a no-op; changing them in the GUI
        # visibly brightens/darkens/gamma-corrects the frame.
        expo, gain, gamma, black = self._photometrics()
        if expo != 1.0 or gain != 1.0 or black != 0:
            frame = np.clip(frame.astype(np.float32) * (expo * gain) + black, 0, 255)
            frame = frame.astype(np.uint8)
        if gamma != 1.0:
            lut = (np.clip((np.arange(256) / 255.0) ** (1.0 / gamma), 0, 1) * 255).astype(np.uint8)
            frame = lut[frame]
        if self.bit_depth > 8:
            # the toy Gaussian spot has no depth to give: its deep frame is the
            # 8-bit one scaled up (enough for the pipeline to run in N bit)
            self._deep = (frame.astype(np.uint16) << (self.bit_depth - 8), self.bit_depth)
        return frame

    def _quantise_both(self, f: np.ndarray) -> np.ndarray:
        """One noisy scene ``f`` (in 8-bit grey levels) -> the 8-bit frame AND
        the full-depth one, both from the same photons, as one camera buffer
        converted twice. The camera parameters act on the light before the
        digitiser here (no double rounding in the deep copy)."""
        expo, gain, gamma, black = self._photometrics()
        if expo != 1.0 or gain != 1.0 or black != 0:
            f = f * (expo * gain) + black
        if gamma != 1.0:
            f = 255.0 * np.clip(f / 255.0, 0.0, 1.0) ** (1.0 / gamma)
        scale = float(1 << (self.bit_depth - 8))
        top = (1 << self.bit_depth) - 1
        self._deep = (np.clip(np.rint(f * scale), 0, top).astype(np.uint16), self.bit_depth)
        return np.clip(np.rint(f), 0, 255).astype(np.uint8)

    def last_deep(self):
        """(full-depth frame, bits) of the last grab, or None in 8-bit mode."""
        return self._deep
