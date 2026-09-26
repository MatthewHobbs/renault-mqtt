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

The shared modules (`util`, `config`, `charge`, `debug`, `mqtt`, `parse`) do **not** live in the
add-on — they come from this package.

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

`main.py` keeps the genuinely per-model logic: `poll_once` (mapping the car's API payloads to the
sensor keys your catalog declares), `run_command` / `COMMAND_ACTIONS`, the control handlers, the
enum-label dicts, `is_charging` / `charging_status_label`, `detect_plug_suspect`, `detect_supported`,
`resolve_account`, the health server, and `main()`. Adapt these from a sibling add-on — the only real
work is `poll_once`, because that's where the car's specific fields land.

Everything else is imported from the core and **wired at startup**:

```python
import catalog
from renault_mqtt import config, mqtt
from renault_mqtt.charge import CHARGES_ENDPOINT, resolve_last_charge, update_charge_session
from renault_mqtt.config import _RedactingFilter, cfg, redact
from renault_mqtt.debug import maybe_dump_api
from renault_mqtt.parse import _bool_on, _charge_schedule_fields, _dist, _enum_label, _find_precond, _hvac_schedule_fields
from renault_mqtt.util import _num, iso, now_ts
from catalog import ENV_PREFIX  # + your tables/constants

# 1) arm the redaction net BEFORE any logging (config fails closed if this is skipped)
config.ENV_PREFIX = ENV_PREFIX
# 2) hand the MQTT seam this model's catalog + identity — MUST come after the ENV_PREFIX line
mqtt.configure(catalog)
```

Then inside `async def main()`, wire the dependency-inversion slots (so the core never imports your
add-on):

```python
loop = asyncio.get_running_loop()
mqtt._LOOP = loop                       # the loop an inbound command is scheduled onto
mqtt._COMMAND_HANDLER = run_command     # your async command dispatcher
...
mqtt._MQTT_CTX["supported"], mqtt._MQTT_CTX["dist_unit"] = supported, dist_unit  # before mqtt_connect()
client = mqtt.mqtt_connect()
mqtt.publish_discovery(client, supported, dist_unit)
```

Read the MQTT topics as attributes (`mqtt.STATE_TOPIC`, `mqtt.AVAIL_TOPIC`, `mqtt.ATTR_TOPIC`,
`mqtt.TRACKER_STATE_TOPIC`) and the location policy as `mqtt.PUBLISH_LOCATION` — `configure()` derives
them at runtime, so don't `from mqtt import STATE_TOPIC` at module load. `resolve_account` sets the
auto-discovered id via `config._DISCOVERED_ACCOUNT_ID = …` so redaction can mask it.

**Optional per-model debug probes.** If the car exposes an endpoint the shared dump doesn't cover
(the R5's raw `alerts` GET), set `debug.EXTRA_SPECIALS = fn` where `fn(vehicle, start, end)` returns an
iterable of `(name, present, call)` tuples. Default `None` = no extras.

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
(this is the R5's situation — see the a290 `r5-entity-name-compat` note). Because HA builds
`entity_id = slug(DEVICE.name + " " + friendly_name)` and ignores `object_id`, entity ids are preserved
as long as your `DEVICE.name` + friendly names match the original. Two escape hatches keep the *topics*
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
- [ ] Dual review (Claude + codex) reconciled.
- [ ] `config.yaml` version + `CHANGELOG.md` entry.
- [ ] Add-on pins the current core SHA; both/all add-ons in lockstep.
