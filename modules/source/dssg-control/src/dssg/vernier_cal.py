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
import os

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
#: optional tables (a calibration averaged over several passes): the
#: pass-to-pass standard deviation of each entry, same shape as its table
_SPREAD_KEYS = ("dev_std_dB", "slope_std_dB_per_count")


def shrink(mean: float, spread: float) -> float:
    """How much of a measured step deviation the module trusts.

    The bench showed ~0.1-0.2 dB run-to-run differences (warm-up drift,
    thermal trends), so a small deviation can be mostly noise -- at 4 GHz,
    -10.5 dBm read -0.01 in one run and +0.15 in the next, and correcting by
    the +0.15 made the level WORSE than not correcting at all. So each mean
    deviation m is weighted by how clearly it stands above its pass-to-pass
    spread s:

        used = m * m^2 / (m^2 + s^2)

    (the weight a least-squares "is this signal or noise?" estimate gives;
    a Wiener filter in one line). Where |m| is much larger than s the
    correction is used in full (10 GHz, -13.5: m 0.63, s 0.14 -> 0.60); where
    the spread is as large as the deviation itself, half of it (a coin flip
    between "real" and "noise"); where the spread dominates, almost nothing --
    the nominal step, which is what we would assume without a calibration.
    It never flips the sign and never makes a correction bigger. With no
    spread (one pass, or s = 0) the mean is used as measured."""
    m, sd = float(mean), abs(float(spread))
    if sd == 0.0 or m == 0.0:
        return m
    return m * m * m / (m * m + sd * sd)


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
                 slope_dB_per_count, meta: dict | None = None, path: str = "",
                 dev_std_dB=None, slope_std_dB_per_count=None):
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
        nf, ns, npw = len(self.freqs_Hz), len(self.steps_dBm), len(self.slope_powers_dBm)
        # pass-to-pass spread (None for a single-pass calibration)
        self.dev_std_dB = (None if dev_std_dB is None
                           else _table("dev_std_dB", dev_std_dB, nf, ns))
        self.slope_std_dB_per_count = (
            None if slope_std_dB_per_count is None
            else _table("slope_std_dB_per_count", slope_std_dB_per_count, nf, npw))
        # what dev() USES: each mean deviation shrunk by its spread (shrink()).
        # The file keeps the measured means; only the use is cautious.
        if self.dev_std_dB is None:
            self.dev_used_dB = [list(r) for r in self.dev_dB]
        else:
            self.dev_used_dB = [[shrink(m, sd) for m, sd in zip(rm, rs)]
                                for rm, rs in zip(self.dev_dB, self.dev_std_dB)]
        self.meta = dict(meta or {})
        self.path = str(path)

    def passes(self) -> int:
        return int(self.meta.get("passes", 1) or 1)

    def worst_spread(self) -> float | None:
        """The largest pass-to-pass spread of any step deviation, dB."""
        if self.dev_std_dB is None:
            return None
        return max(v for row in self.dev_std_dB for v in row)

    # ---- lookups -------------------------------------------------------------

    def in_range(self, frequency_Hz: float) -> bool:
        return self.freqs_Hz[0] <= float(frequency_Hz) <= self.freqs_Hz[-1]

    def dev(self, frequency_Hz: float, attenuator_dBm: float) -> float:
        """dB the step `attenuator_dBm` really sits ABOVE its nominal value."""
        i0, i1, w = _bracket(float(frequency_Hz), self.freqs_Hz)
        d0 = _interp(float(attenuator_dBm), self.steps_dBm, self.dev_used_dB[i0])
        d1 = _interp(float(attenuator_dBm), self.steps_dBm, self.dev_used_dB[i1])
        return d0 + (d1 - d0) * w

    def slope(self, frequency_Hz: float, power_dBm: float) -> float:
        """The vernier's dB per count at this frequency and power (bilinear)."""
        i0, i1, w = _bracket(float(frequency_Hz), self.freqs_Hz)
        s0 = _interp(float(power_dBm), self.slope_powers_dBm, self.slope_dB_per_count[i0])
        s1 = _interp(float(power_dBm), self.slope_powers_dBm, self.slope_dB_per_count[i1])
        return s0 + (s1 - s0) * w

    def describe(self) -> str:
        """One line for the log, the status and the service's stdout: file,
        date, frequency range, passes, worst spread. ASCII only."""
        name = self.path.replace("\\", "/").rsplit("/", 1)[-1] if self.path else "calibration"
        date = str(self.meta.get("date", "?"))[:10]
        spread = self.worst_spread()
        return (f"{name}, measured {date}, "
                f"{self.freqs_Hz[0] / 1e9:g}-{self.freqs_Hz[-1] / 1e9:g} GHz, "
                f"{self.passes()} pass{'es' if self.passes() != 1 else ''}"
                + (f", worst spread {spread:.2f} dB" if spread is not None else ""))

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
        meta = {k: v for k, v in d.items() if k not in _TABLE_KEYS + _SPREAD_KEYS}
        return cls(*(d[k] for k in _TABLE_KEYS), meta=meta, path=path,
                   **{k: d[k] for k in _SPREAD_KEYS if d.get(k) is not None})

    def to_dict(self) -> dict:
        d = {"schema": SCHEMA, "kind": KIND}
        d.update({k: v for k, v in self.meta.items() if k not in ("schema", "kind")})
        d.update({"freqs_Hz": list(self.freqs_Hz), "steps_dBm": list(self.steps_dBm),
                  "dev_dB": [list(r) for r in self.dev_dB],
                  "slope_powers_dBm": list(self.slope_powers_dBm),
                  "slope_dB_per_count": [list(r) for r in self.slope_dB_per_count]})
        if self.dev_std_dB is not None:
            d["dev_std_dB"] = [list(r) for r in self.dev_std_dB]
        if self.slope_std_dB_per_count is not None:
            d["slope_std_dB_per_count"] = [list(r) for r in self.slope_std_dB_per_count]
        return d

    @classmethod
    def load(cls, path) -> "Calibration":
        # encoding="utf-8": the Windows default is cp1252 (gotcha #27)
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh), path=str(path))

    def save(self, path) -> None:
        save_calibration(self.to_dict(), path)


def summary_line(path, fine_power: bool = True) -> str:
    """What the service PRINTS at start about its calibration file (ASCII):
    in use / not usable / not found. Never raises."""
    if not path:
        return "power calibration: none configured (nominal attenuator steps)"
    if not os.path.isfile(path):
        return f"power calibration: no file ({path}) -- nominal attenuator steps"
    try:
        cal = Calibration.load(path)
    except Exception as exc:
        return (f"power calibration: {os.path.basename(path)} NOT usable "
                f"({type(exc).__name__}: {exc}) -- nominal attenuator steps")
    return (f"power calibration: {cal.describe()}"
            + ("" if fine_power else " (NOT used: fine power is off)"))


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


# ---- drift removal and averaging over passes (2026-10-08) ----------------------

def drift_corrected(seq_powers, seq_levels, ref_power: float = -10.0) -> dict:
    """One frequency row of an INTERLEAVED scan -> {power: level - reference}.

    The row visits the reference step again and again ([-10, P1..P10, -10,
    P11.., -10]); the bench drifts by 0.1-0.2 dB over minutes (warm-up,
    temperature), the same for every step. The reference level at any moment
    is interpolated linearly between its neighbouring reference readings (in
    POINT ORDER, a stand-in for time: every point takes about as long), and
    each reading is taken relative to it. What drifts in common cancels;
    what is left is the step's own level. A power read more than once is
    averaged; the reference itself comes out as 0 by construction.
    Readings that are NaN/None are skipped; a row with no finite reference
    reading gives NaN for everything."""
    pw = [float(p) for p in seq_powers]
    lv = [float("nan") if v is None else float(v) for v in seq_levels]
    if len(pw) != len(lv):
        raise ValueError("seq_powers and seq_levels differ in length")
    ref_i = [i for i, p in enumerate(pw)
             if abs(p - ref_power) < 1e-6 and math.isfinite(lv[i])]
    out: dict = {}
    if not ref_i:
        return {round(p, 6): float("nan") for p in pw}
    ref_v = [lv[i] for i in ref_i]
    sums: dict = {}
    for i, (p, v) in enumerate(zip(pw, lv)):
        key = round(p, 6)
        sums.setdefault(key, [])
        if math.isfinite(v):
            sums[key].append(v - _interp(i, ref_i, ref_v))
    for key, vals in sums.items():
        out[key] = sum(vals) / len(vals) if vals else float("nan")
    return out


def average_passes(cals: list) -> dict:
    """Average calibration dicts of several PASSES into one, with the spread.

    Each dict comes from build_calibration on one pass (alternate passes
    sweep the power up and down, so a hysteresis or a thermal trend shows up
    as a pass-to-pass difference instead of a bias). dev and slope are the
    mean over the passes; dev_std_dB / slope_std_dB_per_count are the
    pass-to-pass standard deviations (sample std, n-1) -- what the module's
    shrink() weighs each deviation by, and what tells you which entries are
    real. Only frequencies present in EVERY pass are kept (noted); the steps
    and slope powers must be the same in all passes."""
    if not cals:
        raise ValueError("no passes to average")
    first = cals[0]
    for c in cals[1:]:
        if c["steps_dBm"] != first["steps_dBm"] or c["slope_powers_dBm"] != first["slope_powers_dBm"]:
            raise ValueError("passes differ in their steps or slope powers")
    common = [f for f in first["freqs_Hz"]
              if all(any(abs(f - g) <= 1.0 for g in c["freqs_Hz"]) for c in cals)]
    notes: list = []
    for i, c in enumerate(cals):
        notes += [f"pass {i + 1}: {n}" for n in c.get("notes", [])]
    dropped = [f for c in cals for f in c["freqs_Hz"]
               if not any(abs(f - g) <= 1.0 for g in common)]
    for f in sorted(set(dropped)):
        notes.append(f"{f / 1e9:g} GHz dropped: not in every pass")
    if not common:
        raise ValueError("no frequency was measured in every pass")

    def row(c, key, f):
        j = next(k for k, g in enumerate(c["freqs_Hz"]) if abs(f - g) <= 1.0)
        return c[key][j]

    def stats(key):
        mean, std = [], []
        for f in common:
            rows = [row(c, key, f) for c in cals]
            n = len(rows)
            m = [sum(col) / n for col in zip(*rows)]
            sd = [math.sqrt(sum((v - mu) ** 2 for v in col) / (n - 1)) if n > 1 else 0.0
                  for col, mu in zip(zip(*rows), m)]
            mean.append([round(v, 4) for v in m])
            std.append([round(v, 4) for v in sd])
        return mean, std

    dev, dev_sd = stats("dev_dB")
    slope, slope_sd = stats("slope_dB_per_count")
    d = {k: v for k, v in first.items() if k not in _TABLE_KEYS + _SPREAD_KEYS}
    d.update({"passes": len(cals), "notes": notes, "freqs_Hz": common,
              "steps_dBm": first["steps_dBm"], "dev_dB": dev,
              "slope_powers_dBm": first["slope_powers_dBm"],
              "slope_dB_per_count": [[round(v, 5) for v in r] for r in slope]})
    if len(cals) > 1:
        d["dev_std_dB"] = dev_sd
        d["slope_std_dB_per_count"] = [[round(v, 5) for v in r] for r in slope_sd]
    return d
