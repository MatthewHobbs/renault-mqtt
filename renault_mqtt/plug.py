"""Plug-report consistency using caller-owned persisted state."""


def detect_plug_suspect(state, *, plugged, unplugged, mileage, soc, charging,
                        now, min_age, max_age, km_delta, soc_drop):
    """Return 'on'/'off', preserving the JSON-safe plug baseline in state.

    Callers interpret their plug enum into separate plugged/unplugged flags:
    unknown and error reports set neither. They supply one clock snapshot and
    thresholds in seconds, kilometres and SOC percentage points. Non-plugged
    reports keep the old baseline but arm a fresh baseline on reconnection.
    Missing readings never establish evidence of driving; an incomplete baseline
    is retained until reconnection, as is a present but falsy baseline timestamp.
    State persistence and input validation belong to the caller.
    """
    if plugged and (state.get("plug_prev") != "plugged" or "plug_base_ts" not in state):
        state.update(plug_base_mileage=mileage, plug_base_soc=soc, plug_base_ts=now)
    state["plug_prev"] = "plugged" if plugged else "unplugged"

    drove = False
    bts, bkm, bsoc = state.get("plug_base_ts"), state.get("plug_base_mileage"), state.get("plug_base_soc")
    if bts and None not in (mileage, soc, bkm, bsoc):
        age = now - bts
        if min_age <= age <= max_age:
            drove = (mileage - bkm >= km_delta) and (bsoc - soc >= soc_drop)
    stuck = (plugged and drove) or (unplugged and charging)
    return "on" if stuck else "off"
