"""The physical addresses the camera module claims, in ONE place.

WHY: Lukas's rule -- "the same instrument has to be defined by the same physical
address". Two services must never drive one box at the same time. Every REAL
backend therefore claims the address of the box it is about to open
(``camera.hwlock``), and a second claim of that address -- from this module or
any other -- is refused with a message naming the holder.

What names each box here:

* **a camera** (IDS peak or generic GenICam/Harvester): its SERIAL NUMBER. A USB3
  camera has no VISA resource or COM port, and the two camera backends pick the
  device differently (IDS by serial or display name, Harvester by index or id).
  Claiming the serial of the device ACTUALLY FOUND means "cam1", "0" and
  "4108xxxxxx" all lock the same camera when they resolve to it -- and the IDS
  and GenICam backends collide on the same camera, as they must.
* **a Thorlabs KCube** (own-Z fallback): its Kinesis serial, bare. That is the
  spelling a Kinesis module (zpiezo-control, kim-control) claims, so the camera
  and zpiezo cannot both drive one KCube.

NOT claimed: the remote XY/Z backends (kim / piezo / zpiezo over ZeroMQ). They
send commands to ANOTHER service, which owns and claims the hardware itself.
The simulator claims nothing.
"""

from __future__ import annotations

from .. import hwlock

#: the module key written into the lock file (= describe "module")
MODULE = "camera"


def camera_address(serial) -> str:
    """Lock address of one camera, from its serial number.

    The "CAMERA::" prefix keeps a camera serial from ever colliding with an
    unrelated serial namespace (a Kinesis serial is also just digits).
    """
    s = str(serial).strip()
    if not s:
        raise RuntimeError("the camera reported no serial number; cannot claim it")
    return f"CAMERA::{s}"


def kinesis_address(serial) -> str:
    """Lock address of a Thorlabs Kinesis controller: its serial, as is."""
    s = str(serial).strip()
    if not s:
        raise RuntimeError("no Kinesis serial configured (hardware.kcube_serial)")
    return s


def claim(address: str) -> hwlock.HardwareLock:
    """Claim `address` for the camera module (raises hwlock.HardwareBusy)."""
    return hwlock.claim(address, MODULE)
