"""Wire up a simulated camera system (blueprint §2).

``build_sim_system(cfg)`` returns a ``(brain, camera, xy, z)`` tuple that is fully
wired but NOT started -- the caller decides when to ``brain.start()``.  Every
entry point (service, GUI, smoke test, tests) uses this so they all get an
identical, hardware-free stack whose synthetic scene RESPONDS to motion (so the
stabiliser and autofocus actually converge).
"""

from __future__ import annotations

from .backends.sim import SimCamera, SimXYStage, SimZFocus
from .camera import Camera
from .config import Config


def build_sim_system(cfg: Config | None = None):
    cfg = cfg or Config()
    # Start the stage centred in its travel so both stabiliser directions have room.
    x0 = (cfg.limits.motor_x_min + cfg.limits.motor_x_max) / 2.0
    y0 = (cfg.limits.motor_y_min + cfg.limits.motor_y_max) / 2.0
    xy = SimXYStage(x0=x0, y0=y0)
    z = SimZFocus(z0=cfg.hardware.z_step_v * 30, z_focus=7.6,
                  vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v)
    cam = SimCamera(xy, z,
                    pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um)
    brain = Camera(cam, xy, z, cfg)
    return brain, cam, xy, z


def build_real_system(cfg: Config | None = None):
    """Wire up the REAL stack (drivers imported lazily).

    Camera = IDS peak (or generic GenICam). Motion depends on ``hardware.motion``:
    "kim" = XY and Z both through the kim-control service; "piezo" = XY through
    piezo-control, Z through zpiezo-control or an own KCube.  Kept out of
    :func:`build_sim_system` so the sim path never drags in the hardware libraries.
    """
    from .backends.sim import SimXYStage, SimZFocus

    cfg = cfg or Config()
    h = cfg.hardware
    # camera driver: IDS peak by default (lab: U3-386xCP-M), or generic GenICam/Harvester
    if cfg.camera.driver == "genicam":
        from .backends.genicam import GenICamCamera
        cam = GenICamCamera(device=h.cam_device, video_mode=cfg.camera.video_mode)
    else:
        from .backends.ids import IDSCamera
        cam = IDSCamera(device=h.cam_device or cfg.camera.camera_name)

    if h.motion == "kim":
        from .backends.remote_kim import KimLink, KimXYStage, KimZFocus
        link = KimLink(h.kim_host, h.kim_cmd_port, h.kim_pub_port)
        xy = KimXYStage(link)
        z = KimZFocus(link) if h.use_z else SimZFocus()
        return Camera(cam, xy, z, cfg), cam, xy, z

    from .backends.remote_xy import RemoteXYStage
    xy = (RemoteXYStage(h.piezo_host, h.piezo_cmd_port)
          if h.use_remote_xy else SimXYStage())
    # Z: external service (default) > own KCube > sim (focus disabled)
    if not h.use_z:
        z = SimZFocus()
    elif h.use_remote_z:
        from .backends.remote_z import RemoteZFocus
        z = RemoteZFocus(h.z_host, h.z_cmd_port)
    else:
        from .backends.kcube import KCubeZFocus
        z = KCubeZFocus(h.kcube_serial, cfg.limits.z_min_v, cfg.limits.z_max_v)
    brain = Camera(cam, xy, z, cfg)
    return brain, cam, xy, z
