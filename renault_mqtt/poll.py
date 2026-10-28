"""Shared Kamereon poll payload assembly and location carry-forward for Renault-platform add-ons."""
from renault_mqtt.charge import resolve_last_charge, update_charge_session
from renault_mqtt.hvac import poll_hvac_settings
from renault_mqtt.parse import (
    _bool_on,
    _charge_schedule_fields,
    _dist,
    _enum_label,
    _find_precond,
    _hvac_schedule_fields,
    available_energy,
    location_attrs,
)
from renault_mqtt.util import _num

RHD_LOCALES = frozenset({"en_gb", "en_ie"})
POLL_KEYS = {
    "range": "range",
    "charging_power": "charging_power",
    "charging_remaining": "charging_remaining",
    "plug_status": "plug_status",
    "charging_flap": "charging_flap",
    "charging_status": "charging_status",
    "last_updated": "last_updated",
    "mileage": "mileage",
    "soc_target": "soc_target",
    "soc_min": "soc_min",
}


async def poll_vehicle(
    vsession,
    state,
    capacity_kwh,
    supported_eps,
    dist_unit,
    *,
    plug_status_labels,
    plug_plugged,
    charging_check,
    charging_label,
    breakers,
    breaker_fails,
    breaker_skips,
    breaker_trip,
    breaker_retry_every,
    hvac_due,
    gps_precision,
    latest_data,
    detect_plug_suspect,
    log,
    keys=None,
    cabin_temp=False,
    rhd_locales=RHD_LOCALES,
    publish_location=None,
    update_charge=update_charge_session,
    resolve_charge=resolve_last_charge,
    dump_api=None,
):
    """Assemble one Kamereon poll snapshot and optional coarsened location attributes."""
    k = POLL_KEYS if keys is None else {**POLL_KEYS, **keys}
    vehicle = await vsession.vehicle()
    locale = vsession.locale
    battery = await vehicle.get_battery_status()
    plug = battery.get_plug_status()
    charging = charging_check(battery)
    rhd = locale.lower() in rhd_locales
    data = {
        "battery_level": battery.batteryLevel,
        k["range"]: _dist(battery.batteryAutonomy, dist_unit),
        "battery_temperature": battery.batteryTemperature,
        k["charging_power"]: _num(getattr(battery, "chargingInstantaneousPower", None)),
        k["charging_remaining"]: getattr(battery, "chargingRemainingTime", None),
        "available_energy": available_energy(
            getattr(battery, "batteryAvailableEnergy", None), battery.batteryLevel, capacity_kwh,
        ),
        k["plug_status"]: _enum_label(plug, plug_status_labels, getattr(battery, "plugStatus", None)),
        k["charging_flap"]: "Open: Plugged In" if plug == plug_plugged else "Closed",
        k["charging_status"]: "Charging" if charging else charging_label(battery),
        # The car's own report time, never our clock. Stamping a missing timestamp as now
        # silences the staleness guard in the same way as the GPS sentinel.
        k["last_updated"]: getattr(battery, "timestamp", None),
        "drive_side": "RHD" if rhd else "LHD",
    }
    mileage = None
    try:
        mileage = getattr(await vehicle.get_cockpit(), "totalMileage", None)
        data[k["mileage"]] = _dist(mileage, dist_unit)
    except Exception as err:  # noqa: BLE001
        log.warning("cockpit unavailable: %s", err)
    try:
        hvac = await vehicle.get_hvac_status()
        data["external_temperature"] = getattr(hvac, "externalTemperature", None)
        if cabin_temp:
            data["cabin_temperature"] = getattr(hvac, "internalTemperature", None)
        data["hvac_status"] = str(getattr(hvac, "hvacStatus", ""))
        data["hvac_soc_threshold"] = getattr(hvac, "socThreshold", None)
        data["hvac_last_activity"] = getattr(hvac, "lastUpdateTime", None)
    except Exception as err:  # noqa: BLE001
        log.warning("hvac unavailable: %s", err)
    try:
        sched = await vehicle.get_charge_schedule()
        p = _find_precond(sched)
        data["preconditioning_temperature"] = p.get("preconditioningTemperature")
        data["heated_steering_wheel"] = _bool_on(p.get("preconditioningHeatedStrgWheel"))
        left = p.get("preconditioningHeatedLeftSeat")
        right = p.get("preconditioningHeatedRightSeat")
        data["heated_seat_driver"] = _bool_on(right if rhd else left)
        data["heated_seat_passenger"] = _bool_on(left if rhd else right)
        data.update(_charge_schedule_fields(p))
    except Exception as err:  # noqa: BLE001
        log.warning("ev/settings unavailable: %s", err)
    data.update(await poll_hvac_settings(
        lambda: vehicle.get_hvac_settings(),
        supported_eps,
        breakers,
        breaker_fails,
        breaker_skips,
        trip=breaker_trip,
        retry_every=breaker_retry_every,
        due=hvac_due,
        transform=_hvac_schedule_fields,
        on_unavailable=lambda err: log.warning("hvac-settings unavailable: %s", err),
        on_tripped=lambda n, err: log.warning(
            "hvac-settings has failed %d polls in a row (%s) - pausing it. It "
            "is retried every %d polls and re-enabled automatically if it "
            "recovers.", n, err, breaker_retry_every,
        ),
        on_recovered=lambda: log.info("hvac-settings is answering again - re-enabled."),
    ))
    try:
        soc_lvl = await vehicle.get_battery_soc()
        data[k["soc_target"]] = getattr(soc_lvl, "socTarget", None)
        data[k["soc_min"]] = getattr(soc_lvl, "socMin", None)
    except Exception as err:  # noqa: BLE001
        log.warning("battery_soc unavailable: %s", err)
    if "pressure" in supported_eps:
        try:
            tp = await vehicle.get_tyre_pressure()
            data["tyre_pressure_fl"] = getattr(tp, "flPressure", None)
            data["tyre_pressure_fr"] = getattr(tp, "frPressure", None)
            data["tyre_pressure_rl"] = getattr(tp, "rlPressure", None)
            data["tyre_pressure_rr"] = getattr(tp, "rrPressure", None)
        except Exception as err:  # noqa: BLE001
            log.warning("tyre_pressure unavailable: %s", err)
    if "charge-mode" in supported_eps:
        try:
            cm = await vehicle.get_charge_mode()
            data["charge_mode"] = str(getattr(cm, "chargeMode", "") or "")
        except Exception as err:  # noqa: BLE001
            log.warning("charge_mode unavailable: %s", err)

    loc_attrs = None
    if publish_location is None:
        from renault_mqtt import mqtt

        pub_loc = mqtt.PUBLISH_LOCATION
    else:
        pub_loc = publish_location
    if pub_loc:
        try:
            loc = await vehicle.get_location()
            loc_attrs = location_attrs(loc, gps_precision)
            if loc_attrs is None:
                prev = state.get("gps_last_activity") or (latest_data() or {}).get("gps_last_activity")
                if prev:
                    data["gps_last_activity"] = prev
                    state.setdefault("gps_last_activity", prev)
                if getattr(loc, "gpsLatitude", None) is not None:
                    log.warning(
                        "location fix rejected: the API reported no usable position "
                        "(sentinel or out-of-range coordinates); keeping the last known "
                        "fix and leaving the staleness guard armed"
                    )
            else:
                data["gps_last_activity"] = getattr(loc, "lastUpdateTime", None)
                state["gps_last_activity"] = data["gps_last_activity"]
        except Exception as err:  # noqa: BLE001
            log.warning("location unavailable: %s", err)
            prev = state.get("gps_last_activity") or (latest_data() or {}).get("gps_last_activity")
            if prev:
                data["gps_last_activity"] = prev
                state.setdefault("gps_last_activity", prev)

    live_lc = update_charge(state, battery, capacity_kwh, charging)
    data.update(await resolve_charge(vehicle, state, supported_eps, capacity_kwh, live_lc))
    data["charging"] = "on" if charging else "off"
    data["plug_suspect"] = detect_plug_suspect(state, plug, mileage, battery.batteryLevel, charging)
    if dump_api is None:
        from renault_mqtt.debug import maybe_dump_api as dump_api
    await dump_api(vehicle)
    return data, loc_attrs
