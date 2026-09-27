"""The one piece of scan-core the describe tests need: resolving a settle
`key`, which may be a plain status key or a path such as ["scene", "tone_dBm"].
Copied (not imported) so the module's tests stay independent of scan-core."""


def lookup(st: dict, key):
    if not isinstance(key, (list, tuple)):
        return st.get(key)
    cur = st
    for k in key:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur
