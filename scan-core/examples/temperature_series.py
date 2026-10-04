"""temperature_series.py -- a field x frequency map at each of several temperatures.

The case a fixed recipe cannot express: change the temperature, WAIT until it
has really been stable for a while, then measure; repeat. Every map is its own
file in the data folder, named like the suite names them
(<date>/<time>_map_5K.nc), with the temperature written into the file.

Run it from the scan-core folder:

    uv run python examples/temperature_series.py

As it is, it runs on the SIMULATED instruments. On the rig: set SIMULATE to
False and check the three parameter names below against `lab.summary()`.
"""

from scan_core import api
from scan_core.recipe import Recipe

SIMULATE = True

if SIMULATE:
    from sim_cryostat import add_sim_cryostat       # examples/sim_cryostat.py
    TEMPERATURE, STABLE, FIELD, FREQ, SIGNAL = (
        "temperature", "temperature_stable", "field", "rf_freq", "lockin_r")
    HOLD_S = 1            # how long "stable" must last before measuring
else:
    TEMPERATURE, STABLE, FIELD, FREQ, SIGNAL = (
        "ppms.temperature", "ppms.temperature_stable", "ppms.field",
        "vna.frequency", "vna.s")       # check these with print(lab.summary())
    HOLD_S = 600          # ten minutes

TEMPERATURES_K = [5, 10, 20, 50]

# The map itself: field (outer) x frequency (inner). A recipe saved from the
# Scan tab works just as well here: MAP = "recipes/my_map.yaml".
MAP = Recipe(
    name="map",
    axes=[{"type": "linear", "param": FIELD, "start": 0, "stop": 120, "num": 13},
          {"type": "linear", "param": FREQ, "start": 800, "stop": 2500, "num": 35}],
    detectors=[SIGNAL],
)

with api.connect(simulate=SIMULATE) as lab:
    if SIMULATE:
        add_sim_cryostat(lab)
    for t in TEMPERATURES_K:
        lab.set(TEMPERATURE, t)
        lab.wait_until(STABLE, hold_s=HOLD_S, timeout_s=4 * 3600)
        ds = lab.scan(MAP, name=f"map_{t}K", temperature_K=t)
        lab.log(f"{t} K: strongest signal {float(ds[SIGNAL].max()):.3g}")
    lab.set(FIELD, 0)                   # leave the magnet at zero
