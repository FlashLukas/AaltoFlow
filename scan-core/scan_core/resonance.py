"""resonance.py -- where should the FMR line be? (Kittel, for the resonance window)

Pure numpy/math, no Qt, no sockets, so every number here is tested on its own.
Used by window.py (the engine's "resonance window", 2026-09-28): a slow swept
detector (a spectrum analyser with tracking generator) only needs to sweep a
band around the line, if we can predict where the line is -- and this module
is that prediction, plus its inverse for the self-correction.

Units: fields in mT (mu0 H, i.e. "the field in tesla" times 1000), frequencies
in Hz, angles in degrees. The gyromagnetic ratio is

    gamma' = gamma / 2 pi = g * mu_B / h = g * 13.996 GHz/T = g * 13.996e6 Hz/mT

so g = 2.0023 (free electron) gives 28.02 GHz/T and g = 2.1 (permalloy) 29.4.

Two geometries, the operator picks one (Lukas's decision 3):

OUT-OF-PLANE (field along the film normal, film saturated along it)

    f = gamma' (B - mu0 Meff)            valid only for B > mu0 Meff

  Below mu0 Meff the magnetisation tilts into the plane and the simple
  formula does not hold -- the model says "not valid" and the engine then
  sweeps the full band. The field angle and Hk play no role here.

IN-PLANE (field in the film plane at angle phi_H, uniaxial in-plane
anisotropy Hk with its easy axis at phi_u)

  The magnetisation does NOT simply follow the field when B is comparable to
  Hk: it sits at the angle phi_M that minimises the energy (per moment, in
  field units)

    E(phi) = -B cos(phi_H - phi) - (Hk/2) cos^2(phi - phi_u)

  and the resonance is the Smit-Beljers / Kittel formula at that angle:

    f = gamma' sqrt( H1 * H2 )
    H1 = B cos(phi_H - phi_M) + Hk cos 2(phi_M - phi_u)     (in-plane stiffness)
    H2 = B cos(phi_H - phi_M) + Hk cos^2(phi_M - phi_u) + mu0 Meff

  H1 is exactly d2E/dphi2 at the minimum, so H1 > 0 means "a stable
  single-domain state"; H1 <= 0 (or H2 <= 0) is "not valid". So is a field
  low enough that TWO minima exist (below the Stoner-Wohlfarth switching
  field): which one the sample is in depends on its history, which the model
  does not know -- sweep the full band rather than guess.

  With the magnetisation along the field (B well above Hk, or the field along
  the easy or hard axis) this is exactly vna-control's model.py formula; a
  test checks the two agree number for number.

Meff is the EFFECTIVE magnetisation, Ms minus any perpendicular anisotropy
field -- the one number FMR actually measures, so it is the one the window
tracks.
"""

from __future__ import annotations

import math

#: mu_B / h in Hz per mT (13.996 GHz/T). gamma' = g * this.
MUB_OVER_H_HZ_PER_MT = 13.996e6

MODELS = ("inplane", "outofplane")

#: the material parameters a window accepts, with their defaults
DEFAULT_PARAMS = {"g": 2.0, "meff_mT": 1750.0, "hk_mT": 0.0, "easy_axis_deg": 0.0}


def gamma_hz_per_mT(g: float) -> float:
    """gamma / 2 pi in Hz per mT for a g-factor."""
    return float(g) * MUB_OVER_H_HZ_PER_MT


def _params(params: dict | None) -> dict:
    p = dict(DEFAULT_PARAMS)
    p.update({k: float(v) for k, v in (params or {}).items() if k in DEFAULT_PARAMS})
    return p


def _canonical(B: float, angle_deg: float) -> tuple[float, float]:
    """A negative field at phi is the same field as a positive one at phi+180.

    A 1-axis magnet swept from -100 to +100 mT has angle 0 throughout; the
    magnetisation follows the field round, so only |B| and its direction
    matter. Everything below works with B >= 0.
    """
    if B < 0:
        return -B, angle_deg + 180.0
    return B, angle_deg


def equilibrium_angle(B: float, angle_deg: float, hk_mT: float,
                      easy_axis_deg: float) -> tuple[float, bool]:
    """(phi_M in degrees, unique) for an in-plane field.

    Found ROBUSTLY rather than cleverly: evaluate the energy on a 360-point
    ring, keep every local minimum, polish each with Newton's method on
    dE/dphi. `unique` is False when more than one distinct minimum exists
    (the hysteretic region below the switching field). phi_M is the lowest
    minimum.
    """
    B, angle_deg = _canonical(float(B), float(angle_deg))
    ph = math.radians(angle_deg)
    pu = math.radians(easy_axis_deg)
    hk = float(hk_mT)

    def E(p):
        return -B * math.cos(ph - p) - 0.5 * hk * math.cos(p - pu) ** 2

    def dE(p):
        return -B * math.sin(ph - p) + 0.5 * hk * math.sin(2 * (p - pu))

    def d2E(p):
        return B * math.cos(ph - p) + hk * math.cos(2 * (p - pu))

    n = 360
    grid = [2 * math.pi * k / n for k in range(n)]
    e = [E(p) for p in grid]
    minima = []
    for k in range(n):
        if e[k] <= e[k - 1] and e[k] <= e[(k + 1) % n]:
            p = grid[k]
            for _ in range(50):                 # Newton on dE = 0
                c = d2E(p)
                if c <= 1e-12:
                    break
                step = dE(p) / c
                p -= step
                if abs(step) < 1e-13:
                    break
            if d2E(p) > 0:
                minima.append(p % (2 * math.pi))
    if not minima:
        # A flat energy (B = 0 and Hk = 0): no preferred direction at all.
        return float(angle_deg), False
    # Merge minima that Newton polished onto the same angle (a flat-bottomed
    # well sampled on two neighbouring grid points).
    distinct = []
    for p in sorted(minima, key=E):
        if all(abs(math.remainder(p - q, 2 * math.pi)) > 1e-6 for q in distinct):
            distinct.append(p)
    return math.degrees(distinct[0]), len(distinct) == 1


def kittel_hz(model: str, B_mT: float, angle_deg: float = 0.0,
              params: dict | None = None) -> float:
    """Predicted resonance frequency in Hz, or NaN when the model does not apply."""
    p = _params(params)
    k = gamma_hz_per_mT(p["g"])
    B = float(B_mT)
    if not (math.isfinite(B) and math.isfinite(float(angle_deg))):
        return math.nan
    if model == "outofplane":
        f = k * (abs(B) - p["meff_mT"])
        return f if f > 0 else math.nan
    if model != "inplane":
        raise ValueError(f"unknown resonance model {model!r} (one of {MODELS})")
    h1, h2base, ok = _inplane_terms(B, angle_deg, p)
    if not ok:
        return math.nan
    h2 = h2base + p["meff_mT"]
    if h1 <= 0 or h2 <= 0:
        return math.nan
    return k * math.sqrt(h1 * h2)


def _inplane_terms(B, angle_deg, p) -> tuple[float, float, bool]:
    """(H1, H2 without Meff, valid) at the equilibrium angle. Independent of
    Meff: in-plane, the demagnetising field only stiffens the OUT-of-plane
    precession, it does not move phi_M -- which is what makes the inverse a
    closed formula."""
    Bc, ang = _canonical(B, angle_deg)
    phm, unique = equilibrium_angle(Bc, ang, p["hk_mT"], p["easy_axis_deg"])
    if not unique:
        return math.nan, math.nan, False
    a = math.radians(ang - phm)
    d = math.radians(phm - p["easy_axis_deg"])
    h1 = Bc * math.cos(a) + p["hk_mT"] * math.cos(2 * d)
    h2base = Bc * math.cos(a) + p["hk_mT"] * math.cos(d) ** 2
    return h1, h2base, h1 > 0


def meff_from_f(model: str, f_hz: float, B_mT: float, angle_deg: float = 0.0,
                params: dict | None = None) -> float:
    """The mu0 Meff (mT) that puts the line at f_hz with everything else fixed.

    This is the SELF-CORRECTION: the window measured the dip at f_hz, so this
    is the Meff the sample actually has (as far as the model is right). NaN
    when there is no answer (model not valid at this field, f <= 0).

      out-of-plane:  Meff = B - f / gamma'
      in-plane:      f^2 / gamma'^2 = H1 (H2' + Meff)  ->  Meff = (f/gamma')^2 / H1 - H2'

    Both are closed forms (the in-plane equilibrium does not depend on Meff),
    so no root finding can wander off.
    """
    p = _params(params)
    k = gamma_hz_per_mT(p["g"])
    f = float(f_hz)
    if not (math.isfinite(f) and f > 0 and math.isfinite(float(B_mT))):
        return math.nan
    if model == "outofplane":
        return abs(float(B_mT)) - f / k
    if model != "inplane":
        raise ValueError(f"unknown resonance model {model!r} (one of {MODELS})")
    h1, h2base, ok = _inplane_terms(float(B_mT), float(angle_deg), p)
    if not ok:
        return math.nan
    return (f / k) ** 2 / h1 - h2base


def validate_params(params) -> list[str]:
    """Human-readable problems with a window's material parameters."""
    errs = []
    if params is None:
        return errs
    if not isinstance(params, dict):
        return [f"window params must be a mapping, not {params!r}"]
    for key, value in params.items():
        if key not in DEFAULT_PARAMS:
            errs.append(f"window params: unknown parameter {key!r} "
                        f"(known: {', '.join(DEFAULT_PARAMS)})")
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            errs.append(f"window params: {key} = {value!r} is not a number")
            continue
        if not math.isfinite(v):
            errs.append(f"window params: {key} is not finite")
        elif key == "g" and not (0.5 <= v <= 10):
            errs.append(f"window params: g = {v:g} is not a plausible g-factor")
        elif key == "meff_mT" and not (-5000 <= v <= 5000):
            errs.append(f"window params: meff_mT = {v:g} is outside +-5000 mT")
    return errs
