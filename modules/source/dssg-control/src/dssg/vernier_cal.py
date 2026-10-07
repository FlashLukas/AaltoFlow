"""Fine power: the vernier fills the 0.5 dB gaps of the step attenuator.

Lukas (2026-10-07): "cant we just hide this in the backend and just deliver
power what was asked?" -- yes. The attenuator only makes 0.5 dB steps; the
VERNIER trims the level in raw counts. So a request like -13.73 dBm becomes

    attenuator -13.5 dBm  +  vernier round((-13.73 - -13.5) / slope) counts

and the user never sees the vernier. The remainder is at most half a step
(0.25 dB), i.e. about +-6 counts, where the vernier is close to linear.

THE SLOPE (dB per count) was MEASURED on the lab's SG12000L (firmware V7.84,
2026-10-07, 30 dB pad into a spectrum analyser), a linear fit over -30..+30
counts at -10 dBm. It depends on frequency (table below, interpolated
linearly) and somewhat on power (~0.059 at -20 dBm against 0.044 at -10 and
0 dBm, at 2 GHz). Over a 0.25 dB remainder that costs at most ~0.06 dB, plus
~0.1 dB around 6 GHz, where the vernier steps irregularly.

THE ATTENUATOR STEPS THEMSELVES (2026-10-07, same bench): accurate to ~0.03 dB
at 1-2 GHz, but not above ~4 GHz -- at 10 GHz "-10.5 dBm" is only 0.32 dB below
"-10.0", and "-13.5" falls 0.56 dB short of its nominal 3.5 dB. No vernier
slope fixes that: the step it starts from is already wrong. Hence the PER-UNIT
POWER CALIBRATION below (class Calibration), measured once on the bench with
scripts/calibrate_power.py and kept in dssg_power_calibration.json:

  * dev(f, A): how far the attenuator step A really sits from its nominal
    value, RELATIVE to the step at -10 dBm. Relative, so the cables, the pad
    and the analyser's own flatness cancel out: the absolute level at -10 dBm
    stays the unit's factory calibration, we only straighten the steps.
  * slope(f, P): the vernier's dB per count, measured per frequency AND power.

With a calibration, split() picks the attenuator step whose REAL level is
nearest the request (not the nominally nearest one) and fills the rest with
the vernier, so the level asked for is delivered to ~0.05 dB at every
frequency. Without a file everything behaves exactly as before (nominal steps,
the SLOPE_TABLE below).

This file is pure standard library on purpose: the measurement script imports
it from scan-core's environment, where the dssg package is not installed.
"""

from __future__ import annotations

import json
import math

#: (frequency in Hz, dB per count) measured at -10 dBm, slope over -30..+30
SLOPE_TABLE = ((1.0e9, 0.0480), (2.0e9, 0.0441), (4.0e9, 0.0450),
               (6.0e9, 0.0728), (10.0e9, 0.0600))

#: the remainder is never more than half an attenuator step; this caps the
#: counts so a wrong slope can never push the vernier into its non-linear part
MAX_FILL_COUNTS = 15


def slope_dB_per_count(frequency_Hz: float) -> float:
    """dB per vernier count at this frequency (linear between the measured
    points, the end values outside them)."""
    pts = SLOPE_TABLE
    f = float(frequency_Hz)
    if f <= pts[0][0]:
        return pts[0][1]
    for (f0, s0), (f1, s1) in zip(pts, pts[1:]):
        if f <= f1:
            return s0 + (s1 - s0) * (f - f0) / (f1 - f0)
    return pts[-1][1]


def counts_for(remainder_dB: float, frequency_Hz: float) -> int:
    """Vernier counts that add `remainder_dB` at this frequency."""
    n = int(round(float(remainder_dB) / slope_dB_per_count(frequency_Hz)))
    return max(-MAX_FILL_COUNTS, min(MAX_FILL_COUNTS, n))


def dB_for(counts: int, frequency_Hz: float) -> float:
    """The level change `counts` vernier counts make (small counts only)."""
    return int(counts) * slope_dB_per_count(frequency_Hz)


def split(power_dBm: float, step_dB: float, frequency_Hz: float,
          lo: float, hi: float, cal: "Calibration | None" = None) -> tuple[float, int]:
    """(attenuator setting, vernier counts) for `power_dBm`.

    Without a calibration the attenuator goes to the NEAREST step (so the
    vernier works on at most half a step), kept inside [lo, hi], the safety
    limits. With one, it goes to the legal step whose MEASURED level is
    nearest the request -- at 10 GHz that can be a different step from the
    nominal one -- and the vernier fills the rest with the slope measured
    there."""
    if step_dB <= 0:
        return float(power_dBm), 0
    if cal is not None:
        att = _best_step(float(power_dBm), step_dB, frequency_Hz, lo, hi, cal)
        if att is not None:
            rem = float(power_dBm) - (att + cal.dev(frequency_Hz, att))
            n = int(round(rem / cal.slope(frequency_Hz, att)))
            return att, max(-MAX_FILL_COUNTS, min(MAX_FILL_COUNTS, n))
    att = round(round(power_dBm / step_dB) * step_dB, 6)
    if att < lo - 1e-9:
        att += step_dB
    elif att > hi + 1e-9:
        att -= step_dB
    return att, counts_for(power_dBm - att, frequency_Hz)


def _best_step(target: float, step_dB: float, frequency_Hz: float,
               lo: float, hi: float, cal: "Calibration") -> float | None:
    """The legal attenuator step (a multiple of step_dB inside [lo, hi])
    whose calibrated level A + dev(f, A) is nearest `target`.

    Six steps either side of the nominal one are searched: the measured
    deviations are well under a dB, so the answer is always among them. None
    if no step is legal (a [lo, hi] narrower than a step); the caller then
    falls back to the nominal rule."""
    k0 = round(target / step_dB)
    best, best_err = None, math.inf
    for k in range(k0 - 6, k0 + 7):
        a = round(k * step_dB, 6)
        if a < lo - 1e-9 or a > hi + 1e-9:
            continue
        err = abs(target - (a + cal.dev(frequency_Hz, a)))
        # a tie (within 1 mdB) goes to the nominally nearer step: less vernier
        if err < best_err - 1e-3 or (best is not None and abs(err - best_err) <= 1e-3
                                     and abs(a - target) < abs(best - target)):
            best, best_err = a, min(err, best_err)
    return best


def delivered(attenuator_dBm: float, counts: int, frequency_Hz: float,
              cal: "Calibration | None" = None) -> float:
    """The level we believe comes out of the SMA port for this attenuator
    setting and vernier count: A + dev(f, A) + counts * slope(f, A) with a
    calibration, A + counts * (SLOPE_TABLE) without."""
    if cal is None:
        return float(attenuator_dBm) + dB_for(counts, frequency_Hz)
    a = float(attenuator_dBm)
    return a + cal.dev(frequency_Hz, a) + int(counts) * cal.slope(frequency_Hz, a)


# ---- the per-unit power calibration ------------------------------------------

#: the JSON layout this code writes and reads; a file with another number is
#: refused (a warning, never a crash) rather than guessed at
SCHEMA = 1
KIND = "dssg-power-calibration"
#: the default file name, in the module folder (gitignored: *calibration*.json)
DEFAULT_FILE = "dssg_power_calibration.json"

_TABLE_KEYS = ("freqs_Hz", "steps_dBm", "dev_dB", "slope_powers_dBm",
               "slope_dB_per_count")


def _interp(x: float, xs, ys) -> float:
    """Linear interpolation in an ascending table, the END values outside."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            x0, x1 = xs[i - 1], xs[i]
            return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0)
    return ys[-1]


def _bracket(x: float, xs) -> tuple[int, int, float]:
    """(i0, i1, weight of i1) for linear interpolation, clipped at the ends."""
    if len(xs) == 1 or x <= xs[0]:
        return 0, 0, 0.0
    if x >= xs[-1]:
        return len(xs) - 1, len(xs) - 1, 0.0
    for i in range(1, len(xs)):
        if x <= xs[i]:
            return i - 1, i, (x - xs[i - 1]) / (xs[i] - xs[i - 1])
    return len(xs) - 1, len(xs) - 1, 0.0


def _numbers(name: str, seq, *, ascending: bool = False) -> list[float]:
    if not isinstance(seq, (list, tuple)):
        raise ValueError(f"{name}: not a list")
    try:
        out = [float(v) for v in seq]
    except (TypeError, ValueError):
        raise ValueError(f"{name}: not a list of numbers") from None
    if not out:
        raise ValueError(f"{name}: empty")
    if not all(math.isfinite(v) for v in out):
        raise ValueError(f"{name}: contains NaN or infinity")
    if ascending and any(b <= a for a, b in zip(out, out[1:])):
        raise ValueError(f"{name}: must be strictly ascending")
    return out


def _table(name: str, rows, n_rows: int, n_cols: int) -> list[list[float]]:
    if not isinstance(rows, (list, tuple)) or len(rows) != n_rows:
        raise ValueError(f"{name}: expected {n_rows} rows")
    out = [_numbers(f"{name}[{i}]", r) for i, r in enumerate(rows)]
    if any(len(r) != n_cols for r in out):
        raise ValueError(f"{name}: every row needs {n_cols} values")
    return out


class Calibration:
    """A measured power calibration of ONE generator (see the module doc).

    dev_dB[i][j]             -- step steps_dBm[j] at freqs_Hz[i]: dB above its
                                nominal value, relative to the reference step
    slope_dB_per_count[i][k] -- the vernier at freqs_Hz[i], power
                                slope_powers_dBm[k]

    Both are interpolated LINEARLY in frequency between the measured
    frequencies, with the END values outside (a measurement is never
    extrapolated). The slope is also linear in power, clipped at the ends.
    dev is looked up on the step grid; an off-grid value (there is none in
    normal use) is interpolated between its neighbours.
    """

    def __init__(self, freqs_Hz, steps_dBm, dev_dB, slope_powers_dBm,
                 slope_dB_per_count, meta: dict | None = None, path: str = ""):
        self.freqs_Hz = _numbers("freqs_Hz", freqs_Hz, ascending=True)
        self.steps_dBm = _numbers("steps_dBm", steps_dBm, ascending=True)
        self.dev_dB = _table("dev_dB", dev_dB, len(self.freqs_Hz), len(self.steps_dBm))
        self.slope_powers_dBm = _numbers("slope_powers_dBm", slope_powers_dBm,
                                         ascending=True)
        self.slope_dB_per_count = _table("slope_dB_per_count", slope_dB_per_count,
                                         len(self.freqs_Hz), len(self.slope_powers_dBm))
        if any(v <= 0 for row in self.slope_dB_per_count for v in row):
            # zero would divide by zero in split(); negative would drive the
            # vernier the wrong way
            raise ValueError("slope_dB_per_count: every slope must be > 0")
        self.meta = dict(meta or {})
        self.path = str(path)

    # ---- lookups -------------------------------------------------------------

    def in_range(self, frequency_Hz: float) -> bool:
        return self.freqs_Hz[0] <= float(frequency_Hz) <= self.freqs_Hz[-1]

    def dev(self, frequency_Hz: float, attenuator_dBm: float) -> float:
        """dB the step `attenuator_dBm` really sits ABOVE its nominal value."""
        i0, i1, w = _bracket(float(frequency_Hz), self.freqs_Hz)
        d0 = _interp(float(attenuator_dBm), self.steps_dBm, self.dev_dB[i0])
        d1 = _interp(float(attenuator_dBm), self.steps_dBm, self.dev_dB[i1])
        return d0 + (d1 - d0) * w

    def slope(self, frequency_Hz: float, power_dBm: float) -> float:
        """The vernier's dB per count at this frequency and power (bilinear)."""
        i0, i1, w = _bracket(float(frequency_Hz), self.freqs_Hz)
        s0 = _interp(float(power_dBm), self.slope_powers_dBm, self.slope_dB_per_count[i0])
        s1 = _interp(float(power_dBm), self.slope_powers_dBm, self.slope_dB_per_count[i1])
        return s0 + (s1 - s0) * w

    def describe(self) -> str:
        """One line for the log and the status: file, date, frequency range."""
        name = self.path.replace("\\", "/").rsplit("/", 1)[-1] if self.path else "calibration"
        date = str(self.meta.get("date", "?"))[:10]
        return (f"{name}, measured {date}, "
                f"{self.freqs_Hz[0] / 1e9:g}-{self.freqs_Hz[-1] / 1e9:g} GHz")

    # ---- (de)serialisation ---------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict, path: str = "") -> "Calibration":
        """Build from the JSON dict. Raises ValueError on anything it does not
        recognise -- the brain turns that into a warning and runs without."""
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        if d.get("schema") != SCHEMA:
            raise ValueError(f"schema {d.get('schema')!r}; this code reads schema {SCHEMA}")
        if d.get("kind", KIND) != KIND:
            raise ValueError(f"kind {d.get('kind')!r} is not {KIND!r}")
        missing = [k for k in _TABLE_KEYS if k not in d]
        if missing:
            raise ValueError(f"missing {', '.join(missing)}")
        meta = {k: v for k, v in d.items() if k not in _TABLE_KEYS}
        return cls(*(d[k] for k in _TABLE_KEYS), meta=meta, path=path)

    def to_dict(self) -> dict:
        d = {"schema": SCHEMA, "kind": KIND}
        d.update({k: v for k, v in self.meta.items() if k not in ("schema", "kind")})
        d.update({"freqs_Hz": list(self.freqs_Hz), "steps_dBm": list(self.steps_dBm),
                  "dev_dB": [list(r) for r in self.dev_dB],
                  "slope_powers_dBm": list(self.slope_powers_dBm),
                  "slope_dB_per_count": [list(r) for r in self.slope_dB_per_count]})
        return d

    @classmethod
    def load(cls, path) -> "Calibration":
        # encoding="utf-8": the Windows default is cp1252 (gotcha #27)
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh), path=str(path))

    def save(self, path) -> None:
        save_calibration(self.to_dict(), path)


def save_calibration(d: dict, path) -> None:
    """Write a calibration dict as indented JSON (readable, diffable)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
        fh.write("\n")


# ---- extraction: bench measurement -> calibration ----------------------------

def _fit_slope(counts, levels) -> float | None:
    """Least-squares slope of level against counts (finite points only)."""
    pts = [(float(c), float(v)) for c, v in zip(counts, levels)
           if v is not None and math.isfinite(float(v))]
    if len(pts) < 2:
        return None
    mc = sum(c for c, _ in pts) / len(pts)
    mv = sum(v for _, v in pts) / len(pts)
    sxx = sum((c - mc) ** 2 for c, _ in pts)
    if sxx == 0:
        return None
    return sum((c - mc) * (v - mv) for c, v in pts) / sxx


def _fill_gaps(values: list[float]) -> list[float]:
    """Fill NaN entries of one row linearly from the finite ones (end values
    outside). The row must hold at least one finite value."""
    idx = [i for i, v in enumerate(values) if math.isfinite(v)]
    known = [values[j] for j in idx]
    return [v if math.isfinite(v) else _interp(i, idx, known)
            for i, v in enumerate(values)]


def build_calibration(freqs, powers, levels_A, slope_rows, *,
                      ref_power: float = -10.0, meta: dict | None = None) -> dict:
    """Turn the two bench scans into a calibration dict (schema 1).

    freqs      -- the measured frequencies, Hz (ascending)
    powers     -- the attenuator steps of scan A, dBm (ascending)
    levels_A   -- [f][P] analyser level at vernier 0, dBm (NaN/None = no reading)
    slope_rows -- iterable of (frequency_Hz, power_dBm, counts, levels): scan B,
                  the level at several vernier counts for one setting

    The step deviation is taken RELATIVE to the reference step (ref_power, or
    the nearest step measured):

        dev(f, P) = (L(f, P) - L(f, Pref)) - (P - Pref)

    so dev(f, Pref) = 0, and everything common to all steps at f -- the pad,
    the cables, the analyser's flatness -- cancels. The vernier slope per
    (f, power) is a straight-line fit of level against counts; a setting that
    cannot be fitted falls back to SLOPE_TABLE and is listed in `notes`.
    A frequency without a reading at the reference step is dropped (noted).
    """
    freqs = [float(f) for f in freqs]
    powers = [float(p) for p in powers]
    if any(b <= a for a, b in zip(freqs, freqs[1:])):
        raise ValueError("freqs must be strictly ascending")
    if any(b <= a for a, b in zip(powers, powers[1:])):
        raise ValueError("powers must be strictly ascending")
    if len(levels_A) != len(freqs):
        raise ValueError(f"levels_A has {len(levels_A)} rows for {len(freqs)} frequencies")
    j_ref = min(range(len(powers)), key=lambda j: abs(powers[j] - ref_power))
    p_ref = powers[j_ref]
    notes: list[str] = []

    keep_f, dev = [], []
    for i, f in enumerate(freqs):
        row = [float("nan") if v is None else float(v) for v in levels_A[i]]
        if len(row) != len(powers):
            raise ValueError(f"levels_A row {i}: {len(row)} values for {len(powers)} powers")
        if not math.isfinite(row[j_ref]):
            notes.append(f"{f / 1e9:g} GHz dropped: no reading at the reference step")
            continue
        n_bad = sum(1 for v in row if not math.isfinite(v))
        if n_bad:
            notes.append(f"{f / 1e9:g} GHz: {n_bad} step(s) without a reading, "
                         f"filled from their neighbours")
            row = _fill_gaps(row)
        keep_f.append(f)
        dev.append([round((row[j] - row[j_ref]) - (powers[j] - p_ref), 4)
                    for j in range(len(powers))])
    if not keep_f:
        raise ValueError("no frequency has a reading at the reference step")

    # ---- the vernier slopes, on the (kept frequency x power) grid
    rows = [(float(f), float(p), list(c), list(v)) for f, p, c, v in slope_rows]
    s_pows = sorted({round(p, 6) for _, p, _, _ in rows}) or [p_ref]
    slopes = []
    for f in keep_f:
        srow = []
        for p in s_pows:
            fit = None
            for rf, rp, c, v in rows:
                if abs(rf - f) <= 1.0 and abs(rp - p) <= 1e-6:
                    fit = _fit_slope(c, v)
                    break
            # 0.005..0.5 dB/count brackets every slope measured so far by a
            # factor of ten; outside it the fit saw noise, not the vernier
            if fit is None or not 0.005 <= fit <= 0.5:
                why = "missing" if fit is None else f"{fit:.4f} dB/count implausible"
                notes.append(f"{f / 1e9:g} GHz, {p:g} dBm: vernier slope {why}, "
                             f"nominal table used")
                fit = slope_dB_per_count(f)
            srow.append(round(fit, 5))
        slopes.append(srow)

    d = {"schema": SCHEMA, "kind": KIND}
    d.update(meta or {})
    d.update({"reference_power_dBm": p_ref, "notes": notes,
              "freqs_Hz": keep_f, "steps_dBm": powers, "dev_dB": dev,
              "slope_powers_dBm": s_pows, "slope_dB_per_count": slopes})
    return d
