"""
run_demo.py — prove the N-D engine headlessly.

Runs the example recipes on the simulated registry, saves each result to netCDF,
and renders a figure (2-D map, a 2-D slice of the 3-D cube, and one XY image),
so you can see real structure (a curved resonance line; a patterned sample whose
elements each resonate at their own frequency).

    python run_demo.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from scan_core import Recipe, build_sim_registry, run

HERE = Path(__file__).parent
OUT = HERE / "out"; OUT.mkdir(exist_ok=True)


def main():
    # a fresh registry per recipe → each scan starts from a known baseline
    # (real runs reuse one registry and rely on the recipe's `fixed:` for context)
    # ---- 2-D: field x frequency -----------------------------------------
    reg = build_sim_registry()
    r2 = Recipe.load(HERE / "recipes/field_freq_2d.yaml")
    ds2 = run(r2, reg, created_iso="2026-08-13T00:00:00",
              on_progress=lambda d, t, e: None)
    ds2.to_netcdf(OUT / "field_freq_map.nc")
    print("2-D dims:", dict(ds2.sizes), "->", ds2.attrs["seconds"], "s")

    # ---- 3-D: voltage x field x frequency -------------------------------
    reg = build_sim_registry()
    r3 = Recipe.load(HERE / "recipes/field_freq_voltage_3d.yaml")
    ds3 = run(r3, reg, created_iso="2026-08-13T00:00:00")
    ds3.to_netcdf(OUT / "field_freq_voltage_cube.nc")
    print("3-D dims:", dict(ds3.sizes))

    # ---- XY image vs field (raster) -------------------------------------
    reg = build_sim_registry()
    rxy = Recipe.load(HERE / "recipes/xy_raster_field_3d.yaml")
    dsxy = run(rxy, reg, created_iso="2026-08-13T00:00:00")
    dsxy.to_netcdf(OUT / "xy_image_vs_field.nc")
    print("XY  dims:", dict(dsxy.sizes))

    # ---- one figure summarising all three -------------------------------
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.3))
    ds2["lockin_r"].plot(ax=ax[0], x="rf_freq", y="field", cmap="magma")
    ax[0].set_title("2-D: R(field, freq)")

    ds3["lockin_r"].sel(device_v=4.0, method="nearest").plot(
        ax=ax[1], x="rf_freq", y="field", cmap="magma")
    ax[1].set_title("3-D cube slice @ V=+4")

    dsxy["lockin_r"].isel(field=2).plot(ax=ax[2], x="pos_x", y="pos_y", cmap="viridis")
    ax[2].set_title("XY image @ field[2]")

    fig.tight_layout()
    fig.savefig(OUT / "scan_demo.png", dpi=120)
    print("wrote out/field_freq_map.nc, field_freq_voltage_cube.nc, "
          "xy_image_vs_field.nc, scan_demo.png")


if __name__ == "__main__":
    main()
