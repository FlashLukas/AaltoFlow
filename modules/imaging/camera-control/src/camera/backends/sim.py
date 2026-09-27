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

import threading

import cv2
import numpy as np


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
                 vmax: float = 100.0, up_gain: float = 1.0, down_gain: float = 0.7):
        super().__init__(z0=z0, z_focus=z_focus, vmin=vmin, vmax=vmax)
        self._true = float(z0)
        self.up_gain = float(up_gain)
        self.down_gain = float(down_gain)

    def set_z(self, volts: float) -> None:
        new = float(np.clip(volts, self._vmin, self._vmax))
        delta = new - self._v
        self._true += delta * (self.up_gain if delta > 0 else self.down_gain)
        self._v = new

    def true_z(self) -> float:
        return self._true


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
        self._rng = np.random.default_rng(seed)
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
            "PixelFormat": _F("Pixel Format", "enum", "Mono8", options=["Mono8"], cat="ImageFormat"),
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
        return frame
