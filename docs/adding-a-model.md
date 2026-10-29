# Adding a third Renault-platform model

This package (`renault-mqtt`) holds the code the Renault-platform HA add-ons run in common —
the poll-loop plumbing, MQTT discovery, charge reconciliation, redaction and the value/parse
helpers. Each add-on (Alpine A290, Renault 5, …) is a **thin per-model shim**: its own
`catalog.py` plus a `main.py` that maps that car's API responses to sensors, over this shared core.

Adding a new model — a third Renault-group EV on the same Renault/Kamereon API — means writing a new
add-on that **consumes this package**, not copying an existing one. This guide is the recipe.

> Prerequisite: the car must be supported by the `renault-api` library — i.e. its model code is in
> `renault_api/kamereon/models.py` → `_VEHICLE_ENDPOINTS` (A290 = `A5E1AE`, R5 = `R5E1VE`). That map,
> not the readthedocs pages, is the authoritative source for which endpoints the car exposes. If the
> model isn't there, it can't be polled — stop here.

## 1. Scaffold the add-on

Copy the layout of an existing add-on (start from whichever is closest to your car):

```
<model>/                      the add-on HA installs
  app/
    catalog.py                per-model tables + identity (the bulk of your work)
    main.py                   poll_once field→sensor mapping, controls, wiring (adapt from a sibling)
    deploy.py                 optional dashboard auto-deploy (per-model; copy + adjust)
    requirements.txt          hash-pinned lockfile (renault-api / paho-mqtt / PyYAML)
  config.yaml                 add-on manifest (version, options + schema)
  Dockerfile                  installs requirements.txt + this core (see §4)
  run.sh                      bashio entrypoint; exports <PREFIX>_* env from the options
  tests/                      pytest (conftest + model-specific tests)
```

The shared modules (`util`, `config`, `charge`, `debug`, `mqtt`, `parse`, `freshness`,
`session`, `support`, `plug`, `hvac`, `commands`, `poll`, `runtime`) do **not** live in the
add-on — they come from this package.

### Export options safely from `run.sh`

`run.sh` is the boundary between Home Assistant's add-on options and the Python process. For every
key whose type in `config.yaml`'s `schema:` ends in `?`, export the value through `opt '<key>'`.
Do not export an optional key with raw `bashio::config '<key>'`: when an optional value is unset or
cleared, bashio can emit the literal string `"null"`, while `opt()` normalises that sentinel to the
empty string before any Python reader sees it. Apply the rule to every optional key, including
future `charger_*`-style settings; do not make exceptions because one consumer currently tolerates
`"null"`.

Add a structural test beside the model's other entrypoint tests. It must read the `schema:` from
`config.yaml`, collect every optional (`?`) key, and assert that each is exported through
`opt '<key>'` and never through raw `bashio::config '<key>'`. Also exercise the entrypoint with an
empty options object under a `bashio::config` stub and assert that every optional key is exported as
`""`. This keeps the shell boundary and the schema in lockstep when a model gains a new option.

This contract is recorded in [ADR 0003](adr/0003-normalise-bashio-null-at-the-addon-entrypoint.md);
when referring to it from another repository, cite it as `renault-mqtt/ADR 0003`.

## 2. Write `catalog.py` — the per-model contract

`catalog.py` is the single place your model differs. The core reads the following from it; **required**
unless marked optional. Keep the object-id prefix on every id — the core strips it to form
value-template keys / command suffixes.

### Identity

| Attribute | Type | Notes |
|---|---|---|
| `OBJ_PREFIX` | `str` | e.g. `"scenic_"`. Prefixes every object_id. |
| `ENV_PREFIX` | `str` | e.g. `"SCENIC_"`. The env prefix `run.sh` exports options under. |
| `NODE` | `str` | HA discovery node + MQTT topic root, e.g. `"renault_scenic"`. |
| `DEVICE` | `dict` | HA device block. **Its `name` drives the entity_id slug** — HA ignores `object_id`, and the core pins every entity's `default_entity_id` to `<domain>.slug(device_name + " " + entity_name)`, so an entity's name is part of its id contract (plain ASCII only; `configure()` rejects anything else). Choose deliberately (see §6). |
| `MQTT_KEEPALIVE` | `int` | *Optional*, default `60`. Broker keepalive seconds. |
| `DIST_UNIT_OBJS` | `tuple[str]` | Sensor object_ids whose unit follows the locale (mi/km) instead of a fixed one. |

### Discovery tables

| Attribute | Shape | Purpose |
|---|---|---|
| `SENSORS` | `{object_id: (name, device_class\|None, unit\|None, state_class\|None)}` | Read-only sensors. |
| `BINARY_SENSORS` | `{object_id: (name, device_class\|None)}` | on/off sensors. |
| `ICONS` | `{object_id: "mdi:…"}` | Optional per-entity icon. |
| `OPTIONAL_ENDPOINTS` | `{endpoint_name: [object_id, …]}` | Sensors cleared (and skipped) when their endpoint isn't supported by the car. |
| `RETIRED_SENSORS` | `[object_id, …]` | Ids a previous version shipped; their retained discovery config is cleared on startup. |
| `RETIRED_BINARY_SENSORS` / `RETIRED_BUTTONS` / `RETIRED_NUMBERS` | `[object_id, …]` | *Optional*, default `()`. The same, for that domain. An id may not be both retired and live in one domain; `configure()` raises. |
| `DEFAULT_DISABLED_SENSORS` | `{object_id, …}` | Published but disabled in the entity registry by default. |
| `DATA_GATED_SENSORS` | `{object_id, …}` | *Optional*, default `()`. Sensors whose availability follows whether the poll published their key, so a stalled endpoint shows *unavailable* rather than an empty value. |
| `ACTION_BUTTONS` | `{object_id: (name, icon, endpoint)}` | Control buttons; published only when `endpoint` is supported. |
| `BUTTON_CMD_OVERRIDES` | `{object_id: cmd_suffix}` | *Optional*, default `{}`. Remap a button's command suffix when it differs from `object_id.removeprefix(OBJ_PREFIX)` (see §6). |
| `NUMBERS` | `{object_id: (name, icon, min, max, step)}` | Writable sliders; published only when `SOC_ENDPOINT` is supported. |
| `SOC_ENDPOINT` | `str` | Endpoint gating the charge-limit numbers, e.g. `"soc-levels"`. |
| `REFRESH_LOCATION_EP` | `str` | The refresh-location action endpoint (also suppresses that button when location is opt-out). |

`unique_id` is the object_id, so renaming an object_id creates a **new** entity: put the old id in
the matching `RETIRED_*` list, or the old entity is left behind orphaned.
Retired button and number topics are derived with the **current** `OBJ_PREFIX`, as live ones
are, so the lists cannot retire ids published under a different prefix: never change `OBJ_PREFIX`.

`CHARGES_ENDPOINT` is **not** a catalog value — it's identical across models and lives in
`renault_mqtt.charge`; `main.py` imports it for the endpoint-support probe.

## 3. Wire `main.py`

`main.py` keeps the genuinely per-model adapters and configuration: `poll_once` (delegating to `renault_mqtt.poll.poll_vehicle`
with the sensor keys and optional `cabin_temp` your catalog declares), `run_command` / `COMMAND_ACTIONS`, the control handlers, the
enum-label dicts, `is_charging` / `charging_status_label`, `detect_plug_suspect`, `detect_supported`,
`resolve_account`, `start_health_server`, and `main()` (delegating to `renault_mqtt.runtime`). Adapt these from a sibling add-on.

`detect_plug_suspect` is an app adapter over `renault_mqtt.plug.detect_plug_suspect`.
The app interprets Renault enums into separate `plugged` and `unplugged` flags (neither
for unknown/error), supplies `now_ts()` explicitly, and owns threshold policy: currently
600–43200 seconds inclusive, at least 1 km travelled **and** 2 SOC points lost.
Core owns baseline/reconnect/evidence logic and its pure tests; the app owns persistence
and real-poll contract tests. Pass raw kilometre mileage before display-unit conversion.
The caller-owned JSON state retains `plug_prev`, `plug_base_ts`, `plug_base_mileage`
and `plug_base_soc`. Unknown/error stores the historical `"unplugged"` marker but must
not be treated as an explicit unplugged report for disconnected-but-charging detection.
Missing inputs do not create driving evidence; incomplete/falsy baselines are not
repaired while continuously plugged. Reconnection or an absent timestamp key rebases.

Likewise, apps adapt the shared session lifecycle with their own transport/login,
adapt incremental support resolution with model endpoints, defaults and logging,
adapt `renault_mqtt.hvac` (`hvac_due`, `poll_hvac_settings`) with caller-owned breaker
state (`_BREAKERS`, `_BREAKER_FAILS`, `_BREAKER_SKIPS`), thresholds (`BREAKER_TRIP`,
`BREAKER_RETRY_EVERY`), `vehicle.get_hvac_settings`, and bounded warning/recovery logs,
and adapt `renault_mqtt.commands` (`COMMAND_DEBOUNCE_S`, `command_lock_get`,
`precondition_temp`, `hvac_start_action`, `set_soc_level`, `dispatch_command`) with
caller-owned debounce/in-flight state (`_last_command`, `_in_flight`), short-lived
`aiohttp.ClientSession(timeout=API_TIMEOUT)` + `_login_vehicle` acquisition, and
model-specific `COMMAND_ACTIONS`, `NUMBER_RANGES` and optional `NUMBER_ROLES`.
`poll_once` delegates to `renault_mqtt.poll.poll_vehicle`, passing model label maps,
breaker dicts/thresholds, `GPS_PRECISION`, `_LATEST`, `detect_plug_suspect`, `LOG`,
and optional `keys` / `cabin_temp`.
`resolve_account`, `start_health_server`, `_panel_page`, `_panel_state`, and `main()`
delegate to `renault_mqtt.runtime` (`HEALTH_PORT`, `MILES_LOCALES`, `resolve_account`,
`panel_page`, `panel_state`, `start_health_server`, `is_auth_failure`, `run_main`),
passing model options prefix, `_LATEST`, `_PANEL_FILE`, `VehicleSession`, `SUPPORT_ENDPOINTS`,
`detect_supported`, `deploy.run_deploy`, `poll_once`, `API_TIMEOUT.total`, auth exception
types, and per-model logging/signal/key options.

Everything else is imported from the core and **wired at startup**:

```python
import catalog
from renault_mqtt import config, mqtt
from renault_mqtt.charge import CHARGES_ENDPOINT, _epoch, resolve_last_charge, update_charge_session
from renault_mqtt.config import _RedactingFilter, cfg, redact
from renault_mqtt.debug import maybe_dump_api
from renault_mqtt.parse import (_bool_on, _charge_schedule_fields, _dist, _enum_label, _find_precond,
                                _hvac_schedule_fields, available_energy)
from renault_mqtt.parse import location_attrs as build_location_attrs
from renault_mqtt.util import _num, iso, now_ts
from catalog import ENV_PREFIX  # + your tables/constants

# 1) arm the redaction net BEFORE any logging (config fails closed if this is skipped)
config.ENV_PREFIX = ENV_PREFIX
# 2) hand the MQTT seam this model's catalog + identity — MUST come after the ENV_PREFIX line
mqtt.configure(catalog)
```

Then inside `async def main()`, delegate to `renault_mqtt.runtime.run_main` (which wires `mqtt._LOOP`,
`mqtt._COMMAND_HANDLER`, `mqtt._MQTT_CTX`, `mqtt.mqtt_connect(snapshots=True)`, and incremental
`mqtt.publish_discovery` so the core never imports your add-on):

```python
await run_main(
    addon_name="Renault Scenic", version=VERSION, setup_logging=setup_logging,
    run_command=run_command, start_health_server=start_health_server,
    load_state=load_state, save_state=save_state, latest=_LATEST,
    vsession_factory=VehicleSession, support_endpoints=SUPPORT_ENDPOINTS,
    detect_supported=detect_supported, run_deploy=deploy.run_deploy,
    poll_once=poll_once, api_timeout_total=API_TIMEOUT.total, log=LOG,
    prefix=ENV_PREFIX, miles_locales=MILES_LOCALES,
    auth_exceptions=_AUTH_EXCEPTIONS, http_error_type=aiohttp.ClientResponseError,
    now=now_ts, monotonic=time.monotonic,
    event_factory=asyncio.Event, wait_for=asyncio.wait_for, cfg_fn=cfg,
)
```

Read the MQTT topics as attributes (`mqtt.STATE_TOPIC`, `mqtt.AVAIL_TOPIC`, `mqtt.ATTR_TOPIC`,
`mqtt.TRACKER_STATE_TOPIC`) and the location policy as `mqtt.PUBLISH_LOCATION` — `configure()` derives
them at runtime, so don't `from mqtt import STATE_TOPIC` at module load. `resolve_account` sets the
auto-discovered id via `config._DISCOVERED_ACCOUNT_ID = …` so redaction can mask it.

## 4. Dockerfile — install the core

After the hash-locked `requirements.txt` install and before `COPY app/`, add (copy from a sibling
add-on, keeping the SHA current):

```dockerfile
# renault-mqtt vX.Y.Z
ARG CORE_REF=<immutable-commit-sha>
# git clones the pinned ref; py3-setuptools is the build backend. Both from the apk repo (pinned by
# the base-image @sha256), and --no-build-isolation avoids an unpinned PyPI setuptools fetch, so the
# --require-hashes reproducibility guarantee holds. --no-deps: core's deps are already in the hashed
# tree. Both purged in the same layer so neither ships in the image.
RUN apk add --no-cache --virtual .core-build git py3-setuptools \
 && pip3 install --no-cache-dir --break-system-packages --no-build-isolation --no-deps \
      "renault-mqtt @ git+https://github.com/MatthewHobbs/renault-mqtt@${CORE_REF}" \
 && apk del .core-build
```

Pin by **immutable commit SHA** (keep the tag in the comment for readability). The Dockerfile's
`ARG CORE_REF` is the **single source of truth** for the pinned version.

## 5. CI — install the core, gate on the same checks

In the test job, install the core after the hashed requirements + pytest tooling, reading the SHA
from the Dockerfile so it can never drift from the image:

```yaml
CORE_REF="$(sed -n 's/^ARG CORE_REF=//p' <model>/Dockerfile)"
python3 -m pip install --quiet --no-deps \
  "renault-mqtt @ git+https://github.com/MatthewHobbs/renault-mqtt@${CORE_REF}"
```

Keep the add-on's own gates (ruff, pytest with its coverage floor, bandit, pip-audit, trivy, hadolint,
the HA add-on linter, the image build). Generic behaviour is covered by this package's 100%-gated
suite; your add-on tests cover the model-specific `poll_once` + a **catalog-contract test** (assert the
keys `poll_once` / the core produce match the object_ids your catalog declares).

## 6. If the model must preserve existing entity names

Some models are ports of an existing dashboard "view" whose users' cards query specific `entity_id`s
(this is the R5's situation). Because HA builds
`entity_id = slug(DEVICE.name + " " + friendly_name)` and ignores `object_id`, entity ids are preserved
as long as your `DEVICE.name` + friendly names match the original. One escape hatch keeps the *topics*
right without renaming anything:

- **`BUTTON_CMD_OVERRIDES`** — when a button's command name differs from its entity-id short form
  (R5 ships `object_id "r5_flash_lights"` but commands on `lights`), map `{object_id: cmd_suffix}`. The
  discovery node + object_id still derive from the object_id (entity_id unchanged); only the command
  topic remaps.

**The acceptance gate for an entity-name-sensitive model is a byte-identical discovery diff.** Capture
the current add-on's discovery (before consuming the core) and after, and diff — it must be empty:

```python
# run against the model's mqtt + catalog, all endpoints supported, both location states
all_eps = set(catalog.OPTIONAL_ENDPOINTS) | {catalog.SOC_ENDPOINT, charge.CHARGES_ENDPOINT} \
          | {v[-1] for v in catalog.ACTION_BUTTONS.values()}
class Stub:  # captures publish(topic, payload)
    ...
for loc in (True, False):
    mqtt.PUBLISH_LOCATION = loc
    c = Stub(); mqtt.publish_discovery(c, all_eps, "km")
    for t in sorted(c.pub): print(f"[loc={loc}] {t} => {c.pub[t]}")
```

A non-empty diff means an entity/topic changed — unacceptable.

## 7. When the core can't reproduce the model — extend it, don't rename

The core is proven against the models that exist today. A genuinely new shape may need a new hook.
When it does, follow the pattern the R5 established:

- Make the change **additive and backward-compatible** — a `getattr(catalog, "NEW_HOOK", default)`
  with a default that reproduces the existing models' behaviour, so they're untouched (verify their
  discovery stays byte-identical).
- Prefer **catalog-driven** hooks over branching on model name.
- Give safety-relevant defaults a **fail-closed** value (see `config.ENV_PREFIX = None` → raises rather
  than silently under-redacting; `debug_enabled()` returns `False` when unconfigured).
- Ship it as a new core version, re-pin every add-on, and keep them in lockstep.

Examples in the history: `BUTTON_CMD_OVERRIDES` (v0.9.0) and the `_on_connect` rc-guard (v0.10.0), both
added while wiring the R5.

## 8. Ship checklist

- [ ] `ruff check <model>/app` clean; add-on's full lint suite green.
- [ ] pytest green at the add-on's coverage floor; catalog-contract test present.
- [ ] Container builds via `git+https` with **no unpinned PyPI fetch**, boots, `/healthz` → 200,
      discovery published.
- [ ] If entity-name-sensitive: **byte-identical discovery diff** vs the reference.
- [ ] Second review reconciled.
- [ ] `config.yaml` version + `CHANGELOG.md` entry.
- [ ] Add-on pins the current core SHA; both/all add-ons in lockstep.
