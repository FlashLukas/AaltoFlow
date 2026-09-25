"""view.py -- reducing an N-D scan to the picture asked for.

Lives in aaltoview (`aaltoview.view`) since 2026-09-16, when the data
viewer became its own repository; this module re-exports it so scan-core code
and old scripts keep importing `scan_core.view`. ONE implementation: a fix made
there reaches the suite's live result pane and the viewer alike.
"""

from aaltoview.view import (MODES, PARTS, Reduced, Slice, apply_part,  # noqa: F401
                             coord_text, default_axes, detector, detector_names,
                             is_complex, reduce_cube)
