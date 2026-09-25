"""Measure how the KIM XY stage moves the camera image: direction, px/step, asymmetry.

Needs the kim service (5567) and the camera service (5563, --real) running, with
the stabiliser, autofocus and continuous focus OFF. It MOVES THE STAGE: per axis
and per direction +-100, +-100, +-200, +-400 steps (800 in total), then back to
the start in one move. Z is never touched.

    .venv\\Scripts\\python.exe scripts\\kim_xy_calibration.py [result.json]

How: grab the camera frame after each move, and find where the centre patch of
the START frame now sits in it (normalised template matching). Unlike phase
correlation this survives shifts of hundreds of pixels -- the first attempt
(2026-09-13) used phase correlation, lost track after every 500-step move, and
reported "no motion" for a stage that was moving ~150 px per move.

Reported nm/step uses the camera's CONFIGURED pixel size, which is itself not
calibrated: measure a known scale under the objective to make it absolute.
"""

from __future__ import annotations

import base64
import json
import sys
import time

import cv2
import numpy as np
import zmq

KIM_PORT = 5567
CAMERA_PORT = 5563
PATCH = 360                       # px, centre patch of the start frame
MOVES = (100, 100, 200, 400)      # steps, cumulative 800
SETTLE_S = 0.7                    # after arrival, before grabbing (fresh frames)

ctx = zmq.Context()


def rpc(port, **req):
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.RCVTIMEO, 8000)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(f"tcp://127.0.0.1:{port}")
    try:
        s.send_json(req)
        r = s.recv_json()
    finally:
        s.close()
    if not r.get("ok"):
        raise RuntimeError(f"{req}: {r}")
    return r


def frame():
    png = base64.b64decode(rpc(CAMERA_PORT, cmd="get_frame")["png_b64"])
    return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)


def settled():
    time.sleep(SETTLE_S)
    return frame()


def move(axis, delta):
    """Relative move; returns once kim reports the target reached and stopped."""
    a = "XYZ".index(axis)
    target = rpc(KIM_PORT, cmd="move_steps", axis=axis, delta=delta)["target"]
    t_end = time.monotonic() + 60
    while True:
        st = rpc(KIM_PORT, cmd="status")["status"]
        if not st["moving"][a] and st["position_steps"][a] == target:
            return st["position_steps"]
        if time.monotonic() > t_end:
            rpc(KIM_PORT, cmd="stop")
            raise TimeoutError(f"{axis} did not reach {target}")
        time.sleep(0.05)


def locate(ref, cur):
    """Shift (dx, dy) px of the image content from ref to cur, and match score."""
    h, w = ref.shape
    y0, x0 = h // 2 - PATCH // 2, w // 2 - PATCH // 2
    res = cv2.matchTemplate(cur, ref[y0:y0 + PATCH, x0:x0 + PATCH], cv2.TM_CCOEFF_NORMED)
    _, score, _, (mx, my) = cv2.minMaxLoc(res)

    def sub(v_m, v_0, v_p):         # parabola through the peak -> sub-pixel
        d = v_m - 2 * v_0 + v_p
        return 0.0 if d == 0 else 0.5 * (v_m - v_p) / d

    fx = mx + (sub(res[my, mx - 1], res[my, mx], res[my, mx + 1]) if 0 < mx < res.shape[1] - 1 else 0)
    fy = my + (sub(res[my - 1, mx], res[my, mx], res[my + 1, mx]) if 0 < my < res.shape[0] - 1 else 0)
    return fx - x0, fy - y0, score


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else None
    cam = rpc(CAMERA_PORT, cmd="status")["status"]
    if cam["stabilize_on"] or cam["af_running"] or cam["continuous_focus_on"]:
        sys.exit("switch the stabiliser, autofocus and continuous focus off first")
    px_um = cam["pixel_size_x"]
    start = rpc(KIM_PORT, cmd="status")["status"]["position_steps"]
    print(f"start {start}; pixel size (config, {cam['objective_name']}) {px_um} um/px")
    ref0 = settled()
    print("static check, no move: dx=%+.2f dy=%+.2f score=%.3f" % locate(ref0, settled()))

    log = {}
    try:
        for axis in ("X", "Y"):
            for sign, label in ((+1, "forward"), (-1, "backward")):
                ref = settled()
                rows, cum = [], 0
                print(f"\n{axis} {label}:")
                for d in MOVES:
                    pos = move(axis, sign * d)
                    cum += sign * d
                    dx, dy, sc = locate(ref, settled())
                    rows.append(dict(cum=cum, dx=dx, dy=dy, score=sc, pos=pos))
                    print(f"  cum {cum:+5d} steps -> image ({dx:+8.2f}, {dy:+8.2f}) px, score {sc:.2f}")
                pos = move(axis, -cum)
                dx, dy, sc = locate(ref, settled())
                rows.append(dict(cum=0, dx=dx, dy=dy, score=sc, pos=pos, returned=True))
                print(f"  back ({-cum:+d} in one move) -> residual ({dx:+.2f}, {dy:+.2f}) px")
                log[f"{axis}_{label}"] = rows
    finally:
        print("\nend", rpc(KIM_PORT, cmd="status")["status"]["position_steps"], "start", start)

    print("\n=== linear fits (matches with score > 0.5) ===")
    summary = {}
    for key, rows in log.items():
        pts = [r for r in rows if not r.get("returned") and r["score"] > 0.5]
        if len(pts) < 2:
            print(f"{key}: not enough good matches")
            continue
        c = np.array([0] + [r["cum"] for r in pts], float)
        dx = np.array([0] + [r["dx"] for r in pts])
        dy = np.array([0] + [r["dy"] for r in pts])
        kx, ky = np.polyfit(c, dx, 1)[0], np.polyfit(c, dy, 1)[0]
        pps = float(np.hypot(kx, ky))
        ang = float(np.degrees(np.arctan2(ky * np.sign(c[-1]), kx * np.sign(c[-1]))))
        back = rows[-1]
        summary[key] = dict(px_per_step=pps, nm_per_step=pps * px_um * 1000, image_dir_deg=ang,
                            return_px=[back["dx"], back["dy"]])
        print(f"{key}: {pps:.4f} px/step = {pps * px_um * 1000:.1f} nm/step; image moves "
              f"toward {ang:+.1f} deg (0 = right, +90 = down); return error "
              f"({back['dx']:+.2f}, {back['dy']:+.2f}) px")
    if out:
        json.dump(dict(px_um=px_um, start=start, log=log, summary=summary), open(out, "w"),
                  indent=1, default=float)
        print("saved", out)


if __name__ == "__main__":
    main()
