"""Independent car-payload and successful-poll freshness signals."""

from renault_mqtt.charge import _epoch
from renault_mqtt.util import now_ts


def freshness_fields(state, payload_iso, stale_secs, last_ok):
    """Age car data independently of the feed, preserving the last car timestamp.

    Valid payload timestamps replace state['last_payload_ts']. Missing or invalid
    timestamps reuse it, including during an outage. Without a known timestamp,
    data_stale is omitted so consumers can display unknown. Both signals use a
    strict greater-than threshold; a feed with no successful poll is failing.
    """
    ts = _epoch(payload_iso) if payload_iso else None
    if ts:
        state["last_payload_ts"] = ts
    else:
        ts = state.get("last_payload_ts")
    fields = {"poll_failing": "on" if (now_ts() - last_ok) > stale_secs or not last_ok else "off"}
    if ts:
        fields["data_stale"] = "on" if (now_ts() - ts) > stale_secs else "off"
    return fields
