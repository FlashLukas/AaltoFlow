"""The generator inside the scope's service: its names on the wire.

The generator brain is afg-control's, copied (brain.py): its status keys,
describe ids and verbs are the AFG's. Inside the scope's service they must not
collide with the scope's own (both have "connected", "idn", "hw_error" ...),
so this module puts them in a namespace:

  * status keys and describe ids of an OUTPUT keep their names ("w1_frequency_Hz",
    "w2_settled" -- already unique: the scope's channels are ch1/ch2);
  * everything else gets "gen_" in front ("gen_follow", "gen_outputs_off");
  * verbs get "gen_" in front ("gen_set_frequency {channel: "w1", ...}").

One place, both directions, so the service, describe and the GUI agree.
"""

from __future__ import annotations

import copy

_OUTPUT_PREFIXES = ("w1_", "w2_")


def key(k: str) -> str:
    """A generator status key / describe id -> its name in the scope's service."""
    return k if k.startswith(_OUTPUT_PREFIXES) else "gen_" + k


def status(gen_status: dict) -> dict:
    """The generator's status, namespaced, to merge into the scope's status."""
    return {key(k): v for k, v in gen_status.items()}


def unstatus(scope_status: dict) -> dict:
    """The generator's status back out of a scope status (for the GUI cards,
    which are the AFG's and read the AFG's keys)."""
    out = {}
    for k, v in scope_status.items():
        if k.startswith(_OUTPUT_PREFIXES):
            out[k] = v
        elif k.startswith("gen_"):
            out[k[4:]] = v
    out.setdefault("describe_rev", scope_status.get("describe_rev"))
    return out


def verb(cmd: str) -> str | None:
    """"gen_set_frequency" -> "set_frequency"; None for a scope verb."""
    return cmd[4:] if cmd.startswith("gen_") else None


def params(gen_params: list, group_prefix: str = "Generator") -> list:
    """The generator's describe parameters, namespaced (ids, read paths,
    verbs, settle / wait keys) and grouped under the generator."""
    out = []
    for p in gen_params:
        p = copy.deepcopy(p)
        p["id"] = key(p["id"])
        if p.get("read_path"):
            p["read_path"] = [key(p["read_path"][0])] + list(p["read_path"][1:])
        if p.get("set"):
            p["set"]["verb"] = "gen_" + p["set"]["verb"]
        for blk in (p.get("settle"), (p.get("wait") or {}).get("ready")):
            if blk:
                for k in ("key", "setpoint_key", "flag_key"):
                    if k in blk:
                        blk[k] = key(blk[k])
        if p.get("wait") and "target_key" in p["wait"]:
            p["wait"]["target_key"] = key(p["wait"]["target_key"])
        r = p.get("ramp")
        if r:
            # a sweep: its verbs, its status keys and its record's verbs are
            # the generator's (gen_ramp_start, gen_ramping, gen_stream_read)
            for blk in (r.get("start"), r.get("stop")):
                if blk and blk.get("verb"):
                    blk["verb"] = "gen_" + blk["verb"]
            dn = r.get("done") or {}
            for k in ("key", "id_key"):
                if k in dn:
                    dn[k] = key(dn[k])
            st = (r.get("readback") or {}).get("stream")
            if st:
                st["start_verb"] = "gen_stream_start"
                st["read_verb"] = "gen_stream_read"
                st["stop_verb"] = "gen_stream_stop"
                st["group"] = "gen_" + st.get("group", "ramp")
        g = p.get("group", "")
        p["group"] = g if g in ("W1", "W2") else f"{group_prefix} {g.lower()}".strip()
        out.append(p)
    return out
