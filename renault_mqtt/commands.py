"""Command dispatch, per-group event-loop locks, button debounce and charge-limit writes."""
import asyncio
import math
import time
from contextlib import nullcontext

from renault_mqtt import config
from renault_mqtt.config import cfg, redact
from renault_mqtt.parse import _find_precond

COMMAND_DEBOUNCE_S = 5
_command_locks = {}
_command_lock_loop = None


def command_lock_get(group):
    """Separate climate and charge-limit locks for the active loop, recreated if the loop changes."""
    global _command_lock_loop
    loop = asyncio.get_running_loop()
    if _command_lock_loop is not loop:
        _command_locks.clear()
        _command_lock_loop = loop
    if group not in _command_locks:
        _command_locks[group] = asyncio.Lock()
    return _command_locks[group]


def precondition_temp(option="PRECONDITION_TEMPERATURE", prefix=None):
    """Target cabin temperature (°C) for Start Climate, or None to use the car's own setting."""
    if prefix is None and option == "PRECONDITION_TEMPERATURE" and config.ENV_PREFIX:
        prefix = config.ENV_PREFIX
    key = option if prefix is None else f"{prefix}{option}"
    raw = (cfg(key, "") or "").strip()
    return float(raw) if raw else None


async def hvac_start_action(vehicle, *, get_temp=precondition_temp, default=21.0):
    """Start Climate at the configured temperature, or at the car's own when none is set."""
    temp = get_temp()
    if temp is None:
        try:
            temp = float(_find_precond(await vehicle.get_charge_schedule())
                         .get("preconditioningTemperature") or default)
        except Exception:  # noqa: BLE001
            temp = float(default)
    await vehicle.set_ac_start(temp)


async def set_soc_level(which, payload, *, number_ranges, acquire_vehicle, log,
                        number_roles=None, lock_get=command_lock_get):
    """Validate and write a charge limit while reading and preserving the opposing slider."""
    try:
        number = float(payload)
    except (TypeError, ValueError):
        log.warning("Ignoring non-numeric %s value: %r", which, payload)
        return
    # isfinite first: int(inf) raises OverflowError, outside the except above.
    if not math.isfinite(number) or not number.is_integer():
        log.warning("Ignoring %s value %r: not a whole number", which, payload)
        return
    value = int(number)
    lo, hi, step = number_ranges[which]
    if not lo <= value <= hi or (value - lo) % step:
        log.warning("Ignoring %s=%s: not %s-%s in steps of %s", which, value, lo, hi, step)
        return
    is_min = (number_roles[which] == "min") if number_roles is not None else (which == "soc_min")
    async with lock_get("soc"):
        try:
            async with acquire_vehicle() as vehicle:
                soc = await vehicle.get_battery_soc()
                cur_min = getattr(soc, "socMin", None)
                cur_target = getattr(soc, "socTarget", None)
                new_min, new_target = (value, cur_target) if is_min else (cur_min, value)
                if new_min is None or new_target is None:
                    log.error("Cannot set %s: current limits unavailable (min=%s, target=%s)",
                              which, cur_min, cur_target)
                    return
                if int(new_min) >= int(new_target):
                    log.warning("Ignoring %s=%s: minimum %s%% must be below target %s%%",
                                which, value, new_min, new_target)
                    return
                await vehicle.set_battery_soc(min=int(new_min), target=int(new_target))
            log.info("Set charge limits: min=%s%%, target=%s%%", new_min, new_target)
        except Exception as err:  # noqa: BLE001
            log.error("Failed to set %s=%s: %s", which, value, redact(err))


async def dispatch_command(cmd, payload="", *, number_cmds, set_soc, location_cmds,
                           publish_location, enable_refresh_location, actions,
                           last_command, in_flight, acquire_vehicle, log,
                           climate_cmds=frozenset({"climate_start", "climate_stop"}),
                           debounce_s=COMMAND_DEBOUNCE_S, now=time.monotonic,
                           lock_get=command_lock_get):
    """Dispatch an MQTT button press or writable number command; never fatal."""
    if cmd in number_cmds:
        await set_soc(cmd, payload)
        return
    if cmd in location_cmds and not publish_location:
        log.info("Ignoring '%s' — location is disabled (publish_location: false)", cmd)
        return
    if cmd in location_cmds and not enable_refresh_location:
        log.info("Ignoring '%s' — refresh-location is off (enable_refresh_location: false). "
                 "On a parked car it replaces the car's recorded position with 'no fix' until "
                 "the next completed journey; see DOCS.md before enabling it.", cmd)
        return
    action = actions.get(cmd)
    if action is None:
        log.warning("Ignoring unknown command: %s", cmd)
        return
    # No await between these checks and the marks below, so two presses scheduled together cannot
    # both pass.
    if cmd in in_flight:
        log.info("Ignoring '%s': the previous one is still being sent", cmd)
        return
    # No 0 default: monotonic time starts near zero at boot, so "never pressed" must not read as t=0.
    if cmd in last_command and now() - last_command[cmd] < debounce_s:
        log.info("Ignoring repeated '%s' within %ds (debounce)", cmd, debounce_s)
        return
    last_command[cmd] = now()
    in_flight.add(cmd)
    dispatched = False
    try:
        lock = lock_get("climate") if cmd in climate_cmds else nullcontext()
        async with lock:
            async with acquire_vehicle() as vehicle:
                dispatched = True
                await action(vehicle)
        log.info("Command '%s' sent", cmd)
    except Exception as err:  # noqa: BLE001
        log.error("Command '%s' failed: %s", cmd, redact(err))
    finally:
        # Also on cancellation, so a cancelled press cannot wedge the button. If nothing reached the
        # car, drop the stamp so a retry goes through; once started, the outcome is unknown and a
        # retry could send it twice, so the stamp stays. A press ignored in flight is not replayed.
        in_flight.discard(cmd)
        if not dispatched:
            last_command.pop(cmd, None)
