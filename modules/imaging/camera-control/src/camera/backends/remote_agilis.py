"""AgilisXYStage -- drive XY through the agilis-control SERVICE.

The vacuum setup (2026-11-09): the sample sits on two Newport AG-LS25V6
linear stages on one AG-UC2 controller (axis 1 = X, axis 2 = Y), run by
agilis-control. That module was built from kim and speaks kim's language --
move_to_um / move_to_step / zero_counter, status position_um / position_steps
/ moving / um_per_step_fwd / _bwd / limit_lo / limit_hi -- so the camera reuses
kim's link (one REQ socket, one SUB cache, raw pyzmq, no agilis import) and its
stepping XY adapter (remote_kim.StepXYStage).

The one difference: agilis has NO camera calibration of its own (kim's
move_image_px). So the camera turns an image move into stage um itself, by the
objective's pixel size and hardware.xy_map (which stage axis moves the image
which way). Like kim it is OPEN-LOOP (stick-slip; forward and backward steps
differ): the stabiliser averages only frames taken with the stage at rest, and
the image closes the loop.
"""

from __future__ import annotations

from .remote_kim import KimLink, StepXYStage


class AgilisXYStage(StepXYStage):
    """XYStage over agilis axes X and Y (no image-frame moves: see the docstring)."""


def agilis_link(host: str = "127.0.0.1", cmd_port: int = 5595,
                pub_port: int = 5596, **kw) -> KimLink:
    """A KimLink to agilis-control (its own keyring key, "agilis")."""
    return KimLink(host, cmd_port, pub_port, module="agilis", **kw)
