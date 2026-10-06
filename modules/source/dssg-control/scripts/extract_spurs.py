"""Reduce a harmonics scan to the spur table the GUI draws (sg12000l_spurs.json).

The scan: scan-core, outer axis dssg.power, inner axis dssg.frequency, detector
signalhound.trace (a full spectrum per point). The generator goes through an
attenuator into the analyser; since every level here is RELATIVE to the carrier
in the same trace (dBc), the attenuator and cable loss cancel.

Run (the scan file is NOT in git -- data lives in the lab archive):
    uv run --with xarray --with netcdf4 --with numpy python scripts/extract_spurs.py SCAN.nc
    ... add  --with matplotlib  and  --plot spurs.png  for an overview figure.

What it writes (src/dssg/sg12000l_spurs.json):
  "points": every (set power, carrier) of the scan: carrier level and, per line
            (2f, 3f, f/2, 3f/2), the level in dBc and its margin above the local
            analyser floor -- the measurement itself, kept for the record.
  "model":  what the GUI uses. Per line, segments of adjacent carrier
            frequencies where the line was SEEN; per frequency the level at +5 dBm
            and its slope in dBc per dB of set power, fitted over the powers where
            it was seen. A line that was never above the floor is not in the model:
            the GUI then draws nothing, which means "unknown or below the floor",
            never "clean".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import xarray as xr

LINES = {"2f": 2.0, "3f": 3.0, "f/2": 0.5, "3f/2": 1.5}
WINDOW_GHZ = 0.015        # a line's level = peak of the trace within +-15 MHz
FLOOR_GHZ = (0.04, 0.15)  # local floor = median of 40..150 MHz on both sides
SEEN_DB = 8.0             # a line counts as measured this far above the floor
CARRIER_SEEN_DB = 10.0    # ... and only if the carrier itself was clear
P_REF = 5.0               # model levels are quoted at this set power (dBm)

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent / "src" / "dssg" / "sg12000l_spurs.json"


def _peak(fa, tr, f):
    if f < fa[0] + WINDOW_GHZ or f > fa[-1] - WINDOW_GHZ:
        return None                      # outside the analyser span
    m = (fa > f - WINDOW_GHZ) & (fa < f + WINDOW_GHZ)
    return float(tr[m].max())


def _floor(fa, tr, f):
    lo, hi = FLOOR_GHZ
    m = ((fa > f - hi) & (fa < f - lo)) | ((fa > f + lo) & (fa < f + hi))
    return float(np.median(tr[m])) if m.any() else None


def _model_fw(idn: str) -> str:
    """'DS Instruments,SG12000L,<serial>,V7.84' -> 'SG12000L fw V7.84'."""
    parts = [s.strip() for s in idn.split(",")]
    return f"{parts[1]} fw {parts[3]}" if len(parts) >= 4 else ""


def reduce_scan(path: Path) -> dict:
    ds = xr.open_dataset(path)
    powers = ds["dssg.power"].values.astype(float)
    freqs = ds["dssg.frequency"].values.astype(float) / 1e3   # MHz -> GHz
    fa = ds["signalhound.freq"].values.astype(float)          # GHz
    traces = ds["signalhound.trace"].values

    points = []
    for i, p in enumerate(powers):
        for j, f in enumerate(freqs):
            tr = traces[i, j]
            c = _peak(fa, tr, f)
            c_margin = c - _floor(fa, tr, f)
            pt = {"power_dBm": round(float(p), 2), "f_GHz": round(float(f), 4),
                  "carrier_dBm": round(c, 1), "carrier_margin_dB": round(c_margin, 1),
                  "lines": {}}
            for tag, k in LINES.items():
                h = _peak(fa, tr, k * f)
                if h is None:
                    continue                 # not inside the analyser span
                pt["lines"][tag] = {"dBc": round(h - c, 1),
                                    "margin_dB": round(h - _floor(fa, tr, k * f), 1)}
            points.append(pt)

    model = {}
    for tag in LINES:
        rows = []
        for f in freqs:
            seen = [(pt["power_dBm"], pt["lines"][tag]["dBc"]) for pt in points
                    if pt["f_GHz"] == round(float(f), 4) and tag in pt["lines"]
                    and pt["carrier_margin_dB"] >= CARRIER_SEEN_DB
                    and pt["lines"][tag]["margin_dB"] >= SEEN_DB]
            if not seen:
                rows.append(None)
                continue
            ps, ls = np.array(seen).T
            if len(ps) >= 2:
                slope, icpt = np.polyfit(ps, ls, 1)
            else:
                slope, icpt = 0.0, ls[0]
            rows.append({"f_GHz": round(float(f), 4),
                         "dBc_at_ref": round(float(icpt + slope * P_REF), 1),
                         "slope_dBc_per_dB": round(float(slope), 2),
                         "p_seen_min_dBm": float(ps.min()), "n_seen": int(len(ps))})
        # adjacent seen frequencies form a segment; a lone point is dropped
        # (one frequency gives no extent to draw over)
        segs, cur = [], []
        for r in rows + [None]:
            if r is None:
                if len(cur) >= 2:
                    segs.append(cur)
                cur = []
            else:
                cur.append(r)
        # a frequency seen at only ONE power has no slope of its own: borrow it
        # from the nearest neighbour in the same segment that has one (the
        # mechanism -- divider, amplifier, doubler -- is the same across a band)
        for seg in segs:
            fitted = [r for r in seg if r["n_seen"] >= 2]
            for r in seg:
                if r["n_seen"] < 2 and fitted:
                    near = min(fitted, key=lambda q: abs(q["f_GHz"] - r["f_GHz"]))
                    r["slope_dBc_per_dB"] = near["slope_dBc_per_dB"]
                    r["dBc_at_ref"] = round(r["dBc_at_ref"] + r["slope_dBc_per_dB"]
                                            * (P_REF - r["p_seen_min_dBm"]), 1)
        if segs:
            model[tag] = segs

    snap = json.loads(ds.attrs.get("snapshot_dssg", "{}"))
    return {
        "about": ("Measured spurious lines of the DS Instruments SG12000L. Levels in dBc "
                  "(relative to the carrier in the same spectrum). Produced by "
                  "scripts/extract_spurs.py; edit the scan, not this file."),
        "source": {
            "scan": ds.attrs.get("name", path.stem),
            "created": ds.attrs.get("created", ""),
            # model + firmware only: the *IDN? serial number must not go into git
            "instrument": _model_fw(snap.get("info", {}).get("idn", "")),
            "analyser": "Signal Hound SA124B, 0.48 .. 12.4 GHz, RBW 6 MHz",
            "path": "SG12000L -> 30 dB attenuator -> SA124B",
            # the analyser span: lines outside it were never looked at
            "analyser_GHz": [round(float(fa[0]), 3), round(float(fa[-1]), 3)],
            "powers_dBm": [float(p) for p in powers],
            "carrier_GHz": [round(float(f), 4) for f in freqs],
        },
        "rules": {"window_GHz": WINDOW_GHZ, "floor_GHz": list(FLOOR_GHZ),
                  "seen_dB": SEEN_DB, "carrier_seen_dB": CARRIER_SEEN_DB,
                  "ref_power_dBm": P_REF},
        "model": model,
        "points": points,
    }


def plot(table: dict, out: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ramp = ["#86b6ef", "#5598e7", "#3987e5", "#256abf", "#184f95", "#0d366b"]
    powers = table["source"]["powers_dBm"]
    fig, axs = plt.subplots(2, 2, figsize=(11, 7), sharey=True)
    titles = {"2f": "2nd harmonic (2f)", "3f": "3rd harmonic (3f)",
              "f/2": "f/2 sub-harmonic", "3f/2": "3f/2 spur"}
    for ax, tag in zip(axs.flat, LINES):
        for i, p in enumerate(powers):
            pts = [pt for pt in table["points"] if pt["power_dBm"] == p and tag in pt["lines"]
                   and pt["carrier_margin_dB"] >= CARRIER_SEEN_DB]
            f = np.array([pt["f_GHz"] for pt in pts])
            l = np.array([pt["lines"][tag]["dBc"] for pt in pts])
            seen = np.array([pt["lines"][tag]["margin_dB"] >= SEEN_DB for pt in pts], bool)
            if not len(f):
                continue
            ax.plot(f, np.where(seen, l, np.nan), "-o", color=ramp[i % 6], lw=2, ms=4,
                    label=f"{p:+.0f} dBm")
            ax.plot(f[~seen], l[~seen], "v", mfc="none", mec=ramp[i % 6], ms=5, lw=0)
        ax.set_title(titles[tag], loc="left", fontsize=10)
        ax.grid(color="#e6e5e1", lw=.6); ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.set_xlim(0, 12.2); ax.set_xlabel("carrier frequency (GHz)")
    for ax in axs[:, 0]:
        ax.set_ylabel("level (dBc)")
    axs[0, 0].legend(title="set power", frameon=False, fontsize=8, loc="lower right")
    fig.suptitle(f"SG12000L spurious lines ({table['source']['scan']}): filled = measured; "
                 "open triangle = at the analyser floor (an upper bound)",
                 x=.01, ha="left", fontsize=10)
    fig.tight_layout(); fig.savefig(out, dpi=110)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scan", type=Path, help="the scan-core .nc file")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--plot", type=Path, help="also write an overview figure (PNG)")
    a = ap.parse_args()
    table = reduce_scan(a.scan)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=1)
        fh.write("\n")
    for tag, segs in table["model"].items():
        spans = ", ".join(f"{s[0]['f_GHz']:.2f}-{s[-1]['f_GHz']:.2f}" for s in segs)
        print(f"{tag:5s} seen at carrier {spans} GHz")
    print(f"wrote {a.out}")
    if a.plot:
        plot(table, a.plot)
        print(f"wrote {a.plot}")


if __name__ == "__main__":
    main()
