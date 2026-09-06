"""Shared value-formatting + Kamereon-payload parsing helpers for the Renault-platform add-ons.

These are the model-agnostic helpers the poll loop uses to turn raw API values into HA-ready ones:
unit conversion (`_mi` / `_dist`), boolean/label formatting (`_bool_on` / `_enum_label`), time
normalisation (`_fmt_hhmm` / `_fmt_ready`), and the KCM ev/settings + HVAC-settings summarisers
(`_find_precond` / `_charge_schedule_fields` / `_hvac_schedule_fields`). They were byte-identical
between the A290 and R5 add-ons, so they live here once. Model-specific field→sensor mapping stays
in each add-on's poll_once; these just parse/format the values it feeds in.

Depends only on the util leaf (`_num`).
"""
from renault_mqtt.util import _num

KM_TO_MI = 0.621371


def _mi(km):
    v = _num(km)
    return round(v * KM_TO_MI, 1) if v is not None else None


def _dist(km, unit):
    """Convert a km value to the locale unit ('mi' or 'km')."""
    return _mi(km) if unit == "mi" else _num(km)


def _bool_on(v):
    return "on" if v in (True, "true", "True", "on", "ON", 1, "1") else "off"


def _find_precond(obj, _depth=0):
    """Locate the dict holding preconditioning* fields in the ev/settings payload,
    regardless of how the kcm response nests it."""
    if not isinstance(obj, dict) or _depth > 4:
        return {}
    if any(k.startswith("preconditioning") for k in obj):
        return obj
    for key in ("attributes", "data", "ev"):
        found = _find_precond(obj.get(key), _depth + 1)
        if found:
            return found
    return {}


def _fmt_hhmm(v):
    """A bare 'HHMM' charge time (the KCM format) -> 'HH:MM'; anything else returned as-is."""
    if v is None:
        return None
    s = str(v).strip()
    return f"{s[:2]}:{s[2:]}" if s.isdigit() and len(s) == 4 else (s or None)


def _charge_schedule_fields(settings):
    """KCM ev/settings charge-schedule summary — the chargeModeRq / chargeTimeStart /
    chargeDuration siblings of the preconditioning* fields (field names per renault-api's KCM
    charge-schedule CLI). Absent fields -> None, so a car that doesn't populate them just shows
    the sensors as unavailable rather than erroring. No extra API call: reuses the poll's
    existing get_charge_schedule() payload."""
    mode = settings.get("chargeModeRq")
    return {
        "charge_schedule_mode": mode.replace("_", " ").title() if isinstance(mode, str) and mode else None,
        "scheduled_charge_start": _fmt_hhmm(settings.get("chargeTimeStart")),
        "scheduled_charge_duration": _num(settings.get("chargeDuration")),
    }


_HVAC_DAYS = (("monday", "Mon"), ("tuesday", "Tue"), ("wednesday", "Wed"), ("thursday", "Thu"),
              ("friday", "Fri"), ("saturday", "Sat"), ("sunday", "Sun"))


def _fmt_ready(t):
    """Normalise an HVAC readyAtTime ('T07:00Z' / '0700' / '07:00:00') to 'HH:MM'."""
    if t is None:
        return None
    s = str(t).strip().lstrip("T").rstrip("Z")
    if len(s) >= 5 and s[2] == ":":
        return s[:5]
    return _fmt_hhmm(s)


def _hvac_schedule_fields(settings):
    """Summarise get_hvac_settings() into the climate mode + the active schedule's per-day
    ready times ('Mon 07:00, Fri 08:00'). Reads the typed HvacSettingsData defensively
    (getattr), so a stub / None / no active schedule just yields None values."""
    mode = getattr(settings, "mode", None)
    out = {
        "climate_schedule_mode": mode.replace("_", " ").title() if isinstance(mode, str) and mode else None,
        "climate_ready_time": None,
    }
    active = next((s for s in (getattr(settings, "schedules", None) or [])
                   if getattr(s, "activated", False)), None)
    if active is not None:
        parts = []
        for day, abbr in _HVAC_DAYS:
            ds = getattr(active, day, None)
            t = _fmt_ready(getattr(ds, "readyAtTime", None)) if ds is not None else None
            if t:
                parts.append(f"{abbr} {t}")
        out["climate_ready_time"] = ", ".join(parts) or None
    return out


def _enum_label(enum_val, labels, raw):
    """Friendly label for a decoded enum; fall back to a prettified name, then raw."""
    if enum_val is not None:
        return labels.get(enum_val, enum_val.name.replace("_", " ").title())
    return "Unknown" if raw is None else f"Unknown ({raw})"


def available_energy(raw, soc, capacity_kwh):
    """Usable battery energy in kWh: the car's reported value when present, else a SoC-based
    estimate (soc% of the configured capacity). Several Renault-platform cars don't populate
    batteryAvailableEnergy at all — the R5's battery payload omits it entirely, and the A290's
    is frequently absent — so without this fallback the Available Energy sensor is permanently
    unknown. Returns None only when neither a reported value nor a SoC is available."""
    v = _num(raw)
    if v is not None:
        return v
    if soc is not None:
        return round(soc / 100.0 * capacity_kwh, 2)
    return None


def location_attrs(loc, precision):
    """Build device_tracker attributes from a Kamereon location payload, or None when the
    payload carries no usable fix.

    Kamereon answers "I have no position" with an out-of-band SENTINEL rather than a null or an
    error: an A290 returned gpsLatitude 91 / gpsLongitude 181 on 2026-09-06 — each exactly one
    unit past the maximum — alongside a genuinely fresh lastUpdateTime, because the car had woken
    and honestly reported that it had no fix. The previous `lat is not None and lon is not None`
    check passed that straight through, so the tracker published an impossible coordinate as a
    real position and flipped the car to not_home while it sat on the drive. Worse, the staleness
    guard keys on the timestamp, which WAS fresh, so it disarmed at the same moment.

    Rejecting the payload (rather than clamping it) keeps the last known-good fix on the retained
    topic and leaves the staleness guard armed, which is the honest signal: the position is old,
    and we know it is old.

    0, 0 is rejected too. It is a real coordinate in the Gulf of Guinea and a classic null-island
    sentinel; for a vehicle it is overwhelmingly the latter.
    """
    # NOT _num(): that rounds to 2 dp, which is ~1 km of GPS error and would also round the
    # 90.0001 edge case down to a valid 90.0. Coordinates keep full precision until the caller's
    # own gps_precision rounding.
    def _coord(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v == v and abs(v) != float("inf") else None   # reject NaN / inf

    lat = _coord(getattr(loc, "gpsLatitude", None))
    lon = _coord(getattr(loc, "gpsLongitude", None))
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:
        return None
    return {"latitude": round(lat, precision),
            "longitude": round(lon, precision),
            "gps_accuracy": max(10, round(111_000 / 10 ** precision)),
            "last_update": getattr(loc, "lastUpdateTime", None)}
