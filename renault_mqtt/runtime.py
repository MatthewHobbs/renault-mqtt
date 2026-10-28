"""Account resolution, health/status panel HTTP server, auth-error classification and main poll-loop orchestration."""
import asyncio
import os
import signal
import sys
import time

from renault_mqtt import config
from renault_mqtt.config import cfg, redact
from renault_mqtt.freshness import freshness_fields
from renault_mqtt.util import iso, now_ts

HEALTH_PORT = 8099
HEALTH_GRACE = 60
MILES_LOCALES = frozenset({"en_gb"})
_AUTH_KEYWORDS = ("login", "password", "credential", "401", "403")


def _mqtt():
    from renault_mqtt import mqtt

    return mqtt


async def resolve_account(client, *, log, prefix=None, cfg_fn=cfg):
    """Resolve the Kamereon account id from config, VIN garage match, or MYRENAULT fallback."""
    if prefix is None:
        prefix = config.ENV_PREFIX or ""
    account_id = cfg_fn(f"{prefix}ACCOUNT_ID")
    if account_id:
        return account_id
    vin = (cfg_fn(f"{prefix}VIN") or "").strip().upper()
    person = await client.get_person()
    fallback = None
    for account in person.accounts:
        if vin:
            try:
                api_account = await client.get_api_account(account.accountId)
                vehicles = await api_account.get_vehicles()
                garage = {(getattr(link, "vin", "") or "").upper()
                          for link in (getattr(vehicles, "vehicleLinks", None) or [])}
                if vin in garage:
                    config._DISCOVERED_ACCOUNT_ID = account.accountId
                    log.info("Auto-discovered %s account (its garage contains the configured VIN)",
                             account.accountType)
                    return account.accountId
            except Exception as err:  # noqa: BLE001 — one bad account must not abort discovery
                log.debug("Garage lookup for the %s account failed: %s", account.accountType, err)
        if account.accountType == "MYRENAULT" and fallback is None:
            fallback = account.accountId
    if fallback is not None:
        config._DISCOVERED_ACCOUNT_ID = fallback
        log.warning("No account garage matched the configured VIN; falling back to the MYRENAULT "
                    "account. If polling fails, set account_id on the Configuration page.")
        return fallback
    raise RuntimeError(f"No account contains the configured VIN and {prefix}ACCOUNT_ID is not set")


async def panel_page(panel_file, *, web_mod=None):
    """Serve the self-contained, read-only ingress status panel HTML or a plain-text fallback."""
    if web_mod is None:
        from aiohttp import web as web_mod

    if os.path.isfile(panel_file):
        return web_mod.FileResponse(panel_file)
    return web_mod.Response(text="Status panel unavailable", content_type="text/plain")


async def panel_state(latest, *, web_mod=None):
    """Serve the latest car state + diagnostics JSON for the status panel."""
    if web_mod is None:
        from aiohttp import web as web_mod

    return web_mod.json_response(latest)


async def start_health_server(
    *, panel_file, latest, log, port=HEALTH_PORT, panel_page_fn=None, panel_state_fn=None, web_mod=None,
    health_state=None, now_monotonic=time.monotonic,
):
    """Start /healthz and the read-only ingress status panel (GET / and GET /api/state).

    `health_state` carries the poll loop's expected-progress deadline (monotonic seconds).
    A deadline that has passed answers 503; while it is None (startup) /healthz answers 200.
    """
    if web_mod is None:
        from aiohttp import web as web_mod

    async def _healthz(_req):
        deadline = (health_state or {}).get("deadline")
        if deadline is not None and now_monotonic() >= deadline:
            return web_mod.Response(text="stalled", status=503)
        return web_mod.Response(text="ok")

    async def _page(_req):
        p = panel_file() if callable(panel_file) else panel_file
        return await panel_page(p, web_mod=web_mod)

    async def _state(_req):
        return await panel_state(latest, web_mod=web_mod)

    app = web_mod.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/", _page if panel_page_fn is None else panel_page_fn)
    app.router.add_get("/api/state", _state if panel_state_fn is None else panel_state_fn)
    runner = web_mod.AppRunner(app)
    await runner.setup()
    await web_mod.TCPSite(runner, "0.0.0.0", port).start()  # nosec B104
    log.info("Health endpoint + status panel listening on :%d", port)
    return runner


def is_auth_failure(err, *, auth_exceptions=(), http_error_type=None):
    """Classify whether a poll exception represents an authentication/authorization failure."""
    return (
        (bool(auth_exceptions) and isinstance(err, auth_exceptions))
        or (http_error_type is not None and isinstance(err, http_error_type) and getattr(err, "status", None) in (401, 403))
        or any(s in str(err).lower() for s in _AUTH_KEYWORDS)
    )


async def run_main(
    *,
    addon_name,
    version,
    setup_logging,
    run_command,
    start_health_server,
    load_state,
    save_state,
    latest,
    vsession_factory,
    support_endpoints,
    detect_supported,
    run_deploy,
    poll_once,
    api_timeout_total,
    log,
    prefix=None,
    miles_locales=MILES_LOCALES,
    plug_key="plug_status",
    last_updated_key="last_updated",
    log_duration=False,
    early_signals=False,
    auth_exceptions=(),
    http_error_type=None,
    now=now_ts,
    monotonic=time.monotonic,
    event_factory=asyncio.Event,
    wait_for=asyncio.wait_for,
    cfg_fn=cfg,
):
    """Run the add-on startup sequence, bounded poll loop with backoff, and clean shutdown."""
    if prefix is None:
        prefix = config.ENV_PREFIX or ""
    setup_logging()
    log.info("%s add-on v%s starting", addon_name, version)
    for req in (f"{prefix}USERNAME", f"{prefix}PASSWORD", f"{prefix}VIN", "MQTT_HOST"):
        if not cfg_fn(req):
            log.error("Missing required setting: %s — set it on the add-on Configuration page.", req)
            sys.exit(1)

    locale = cfg_fn(f"{prefix}LOCALE", "en_GB")
    dist_unit = "mi" if locale.lower() in miles_locales else "km"
    interval = int(cfg_fn(f"{prefix}POLL_INTERVAL", "300") or "300")
    capacity = float(cfg_fn(f"{prefix}BATTERY_CAPACITY_KWH", "52") or "52")
    stale_secs = int(cfg_fn(f"{prefix}STALE_HOURS", "6") or "6") * 3600

    mqtt = _mqtt()
    loop = asyncio.get_running_loop()
    mqtt._LOOP = loop
    mqtt._COMMAND_HANDLER = run_command
    stop = None
    if early_signals:
        stop = event_factory()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

    poll_budget = max(30, interval - 10)
    health_state = {"deadline": None}
    health = await start_health_server(health_state=health_state)
    state = load_state()
    last_data = {}
    latest.update(ok=False, last_poll=None, data={})
    vsession = vsession_factory(locale)
    unresolved = set(support_endpoints)
    supported = set()
    try:
        supported = await wait_for(detect_supported(vsession, unresolved=unresolved),
                                   timeout=poll_budget)
    except asyncio.TimeoutError:
        log.warning("Endpoint detection timed out; polling without discovery until it succeeds")
    mqtt._MQTT_CTX["supported"], mqtt._MQTT_CTX["dist_unit"] = supported, dist_unit
    latest["supported"] = sorted(supported)
    latest["dist_unit"] = dist_unit
    client = mqtt.mqtt_connect(snapshots=True)
    await run_deploy(refresh_location=bool(mqtt.PUBLISH_LOCATION and mqtt.ENABLE_REFRESH_LOCATION))

    if stop is None:
        stop = event_factory()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

    fails = 0
    max_backoff = max(interval, 1800)
    health_state["deadline"] = monotonic() + poll_budget + HEALTH_GRACE
    while not stop.is_set():
        try:
            t0 = monotonic()
            data, location_attrs = await wait_for(
                poll_once(vsession, state, capacity, supported, dist_unit),
                timeout=max(30, interval - 10))
            if unresolved:
                previous = set(supported)
                try:
                    await wait_for(detect_supported(vsession, supported, unresolved),
                                   timeout=min(api_timeout_total, max(30, interval - 10)))
                except Exception as err:  # noqa: BLE001
                    log.warning("Endpoint-support retry failed: %s", redact(err))
                if supported != previous:
                    mqtt._MQTT_CTX["supported"] = supported
                    latest["supported"] = sorted(supported)
                    mqtt.publish_discovery(client, supported, dist_unit)
            fails = 0
            state["last_success"] = now()
            data["api_auth_failure"] = "off"
            data["last_successful_poll"] = iso(state["last_success"])
            data.update(freshness_fields(state, data.get(last_updated_key), stale_secs,
                                         state["last_success"]))
            last_data = data
            latest.update(ok=True, last_poll=iso(now()), data=data)
            if location_attrs:
                client.snapshots.update(data, location=location_attrs)
            else:
                client.snapshots.update(data)
            save_state(state)
            if log_duration:
                log.info("Published in %.1fs: %s%% battery, plug=%s, charging=%s, suspect=%s",
                         monotonic() - t0, data.get("battery_level"),
                         data.get(plug_key), data.get("charging"), data.get("plug_suspect"))
            else:
                log.info("Published: %s%% battery, plug=%s, charging=%s, suspect=%s",
                         data.get("battery_level"), data.get(plug_key),
                         data.get("charging"), data.get("plug_suspect"))
        except Exception as err:  # noqa: BLE001
            fails += 1
            log.error("Poll failed (%d in a row): %s", fails, redact(err))
            last_ok = state.get("last_success", 0)
            fresh = freshness_fields(state, None, stale_secs, last_ok)
            auth = is_auth_failure(err, auth_exceptions=auth_exceptions, http_error_type=http_error_type)
            if auth or fails % 3 == 0:
                await vsession.invalidate()
            data = {
                **last_data,
                "api_auth_failure": "on" if auth else "off",
                "last_successful_poll": iso(last_ok),
                **fresh,
            }
            client.snapshots.update(data)
            latest.update(ok=False, last_poll=iso(now()), error=redact(err), data=data)
            save_state(state)
        wait = interval if fails == 0 else min(interval * 2 ** (fails - 1), max_backoff)
        # The loop is alive if an iteration completed, success or failure; the deadline covers
        # the sleep and the next poll, so only a loop that stops completing reports stalled.
        health_state["deadline"] = monotonic() + wait + poll_budget + HEALTH_GRACE
        try:
            await wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass

    log.info("Shutting down")
    await vsession.close()
    await health.cleanup()
    await client.snapshots.close()
