"""sample_positions.py -- the same measurement on several dies of one chip.

A list of positions (one per die), and at each: move there, focus, run one
scan. Every die gets its own file, named after it, with its position inside.

Run it from the scan-core folder:

    uv run python examples/sample_positions.py

As it is, it runs on the SIMULATED instruments (whose sample has a pattern of
discs and bars -- each "die" here sits on a different one). On the rig:
SIMULATE = False, the stage's names from `print(lab.summary())`, and positions
read off the camera or the Navigator tab.
"""

from scan_core import api
from scan_core.recipe import Recipe

SIMULATE = True

if SIMULATE:
    X, Y, FOCUS = "pos_x", "pos_y", "sim_autofocus"
    FIELD, FREQ, SIGNAL = "field", "rf_freq", "lockin_r"
else:
    X, Y, FOCUS = "kim.position_x", "kim.position_y", "camera.autofocus"
    FIELD, FREQ, SIGNAL = "clMag.field", "smb.frequency", "hf2.r1"

# name: (x, y) in the stage's unit (um)
DIES = {
    "die_A1": (-26.0, 22.0),
    "die_A2": (2.0, 27.0),
    "die_B1": (0.0, 0.0),
    "die_B2": (27.0, -8.0),
}

# One frequency sweep at three fields, on every die.
SWEEP = Recipe(
    name="die sweep",
    axes=[{"type": "array", "param": FIELD, "values": [20, 60, 100]},
          {"type": "linear", "param": FREQ, "start": 800, "stop": 2500, "num": 69}],
    detectors=[SIGNAL],
)

with api.connect(simulate=SIMULATE) as lab:
    for die, (x, y) in DIES.items():
        lab.log(f"--- {die} at ({x:g}, {y:g}) ---")
        lab.set(X, x)
        lab.set(Y, y)
        try:
            lab.run(FOCUS)
        except api.ActionFailed as exc:
            lab.log(f"{die}: focus failed ({exc}); skipping this die")
            continue
        lab.scan(SWEEP, name=die, die=die, x_um=x, y_um=y)
