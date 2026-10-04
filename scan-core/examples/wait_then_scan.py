"""wait_then_scan.py -- wait for a condition, focus, then run a saved recipe.

For an overnight start: cool down, wait until the temperature is below a
threshold AND has stayed there, focus the camera, take a reference, and only
then start the measurement -- a recipe saved from the Scan tab.

Run it from the scan-core folder:

    uv run python examples/wait_then_scan.py

As it is, it runs on the SIMULATED instruments. On the rig: SIMULATE = False,
and the names below from `print(lab.summary())`.
"""

from pathlib import Path

from scan_core import api

SIMULATE = True
HERE = Path(__file__).resolve().parent

if SIMULATE:
    from sim_cryostat import add_sim_cryostat       # examples/sim_cryostat.py
    TEMPERATURE, FOCUS, REFERENCE = "temperature", "sim_autofocus", "vna_reference"
    HOLD_S = 1
else:
    TEMPERATURE, FOCUS, REFERENCE = ("ppms.temperature", "camera.autofocus",
                                     "vna.take_reference")
    HOLD_S = 300

# Any recipe saved from the Scan tab (or a measured .nc: its definition is reused)
RECIPE = HERE.parent / "recipes" / "field_freq_2d.yaml"

with api.connect(simulate=SIMULATE) as lab:
    if SIMULATE:
        add_sim_cryostat(lab)
    lab.set(TEMPERATURE, 4.0)
    # below 4.2 K, and staying below it for HOLD_S seconds
    lab.wait_until(f"{TEMPERATURE} < 4.2", hold_s=HOLD_S, timeout_s=6 * 3600)
    try:
        lab.run(FOCUS)
    except api.ActionFailed as exc:
        # A failed focus is worth knowing about, not worth losing the night
        # for: say it in the log and measure anyway.
        lab.log(f"focus failed ({exc}); measuring anyway")
    lab.run(REFERENCE)
    ds = lab.scan(RECIPE, name="overnight map",
                  comment="started by examples/wait_then_scan.py")
    lab.log(f"saved to {api.path_of(ds)}")
