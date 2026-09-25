"""data.py -- reading back what the engine wrote.

Complex detectors are stored as `<id>_real` + `<id>_imag` (the engine writes
them that way so MATLAB and Igor can read the file; see engine.py). The READING
side lives in aaltoview (`aaltoview.data`) since 2026-09-16 and is
re-exported here, so `from scan_core.data import as_complex, load` still works.

The file layout is the contract between the two repositories: change what the
engine writes and aaltoview.data together.

    from scan_core.data import as_complex, load
    ds = load("out/fmr_map.nc")
    s21 = as_complex(ds, "s21")          # complex DataArray (field, vna_freq)
"""

from aaltoview.data import (Summary, as_complex, complex_names,  # noqa: F401
                             find_measurements, load, summarize, to_complex_dataset)
