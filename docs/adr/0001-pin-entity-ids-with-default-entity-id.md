# ADR 0001 — Pin entity ids with default_entity_id

- **Status:** Proposed. The owner confirmed the four decisions below on 2026-09-26; this record
  awaits his acceptance, which is what authorises the Open rows.
- **Context:** r5-ha-addon issue #83: `number.auto_r5_soc_max_target` instead of
  `number.r5_soc_max_target`, because Home Assistant put the device's area into a new entity's
  id. Proposed as [RFC 0005: pin MQTT entity ids with default_entity_id](https://claude.ai/code/artifact/e4bf159f-2c74-4efd-a64c-aa29e68c914f).
- **North star:** every entity the add-ons publish gets the same id on every install, whatever
  area or name the user gives the car's device, and that id is the one the dashboards and docs use.

<!-- Format: claude-config docs/adr/0000-template.md, referenced at source rather than copied.
The Decision table is the plan and is kept current; the rest is the dated record. -->

## Decision

The core sends `default_entity_id` in every discovery config it publishes (sensor, binary_sensor,
button, number and the device_tracker), set to the **name-derived** id,
`<domain>.<slug(device name + " " + entity name)>`, and keeps sending `object_id`.

Decisions the owner confirmed on 2026-09-26:

1. **Option A** of RFC 0005 (see Alternatives).
2. **The minimum supported Home Assistant is 2026.8.1**, which both add-ons declare in
   `config.yaml` (`homeassistant: "2026.8.1"`; read on 2026-09-26 from a290-ha-addon 1.28.2
   and r5-ha-addon 1.8.4).
3. **Release order:** this core first, then a290-ha-addon bumps `CORE_REF` and releases, then
   r5-ha-addon. This replaces the RFC's proposed order (core, r5, a290).
4. **The device_tracker is included**, not only the four entity tables.

| # | Step | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| 1 | `publish_discovery` sends `default_entity_id` = name-derived id on all five domains; keeps `object_id`; `configure()` rejects a name whose id the core cannot derive; tests pin the slug rule | renault-mqtt | Open | Branch `feat-default-entity-id`; PR not yet opened |
| 2 | **End-to-end experiment at HA 2026.8.1 and at current stable.** Scenarios: (a) the pin works with the device in an area; (b) a plain install is unchanged, for the real a290 and r5 catalogs. **PENDING; gates the merge of row 1.** | renault-mqtt (e2e harness) | Open | **TODO: results not yet run. Record the HA versions, the ids read from the registry, and what a failing result would have looked like. Do not fill from expectation.** |
| 3 | Core release: `__version__` bump in its own PR; the release workflow tags it | renault-mqtt | Open | |
| 4 | a290-ha-addon: `CORE_REF` bump, conformance test reads `default_entity_id`, patch release, container boot | a290-ha-addon | Open | |
| 5 | r5-ha-addon: the same, after row 4 | r5-ha-addon | Open | |

Status is one of **Open**, **Done**, **Blocked**, **Dropped**. A **Done** row carries Evidence.

## Context (2026-09-26)

Home Assistant ignores the `object_id` in an MQTT discovery config. It builds a new entity's id
from the device's area, the device's name (a user rename wins) and the entity's name. An R5
device in an area called "Auto" therefore gets `number.auto_r5_soc_max_target`, and the bundled
dashboards, which reference `number.r5_soc_max_target`, show "Entity not found".

### Evidence carried from RFC 0005

Measured end to end on 2026-09-25 on **HA 2026.7.1 and 2026.9.3**: a fresh Home Assistant
container and a Mosquitto broker, the MQTT integration set up through its config flow, and core
v0.17.0's real discovery payloads published over MQTT, with the entity ids read from HA's
registry over websocket. Each scenario used its own device, all named "R5", so later ones collide
and get a `_2` suffix.

| Scenario | HA 2026.7.1 | HA 2026.9.3 |
| --- | --- | --- |
| 1 Control: device "R5", no area | `number.r5_soc_max_target` | `number.r5_soc_max_target` |
| 2 Device in area "Auto" before discovery | `number.auto_r5_soc_max_target` | `number.auto_r5_soc_max_target` |
| 3 Device renamed "Auto R5" before discovery | `number.auto_r5_soc_max_target_2` | `number.auto_r5_soc_max_target_2` |
| 4 Registered first, then area "Auto", then discovery re-sent | unchanged (`number.r5_soc_max_target_2`) | unchanged (`number.r5_soc_max_target_2`) |
| 5 Area "Auto" + `default_entity_id` in the payload | `number.r5_pinned_soc_max_target` | `number.r5_pinned_soc_max_target` |

What that establishes:

- **`object_id` is ignored on both versions.** Scenario 2's payload carried
  `object_id: r5_soc_max_target`, and HA used the area prefix anyway.
- **The behaviour is not new in 2026.9.** Both versions do the same thing.
- **Existing entities keep their ids** (scenario 4). Only entities HA registers *after* the device
  has an area or a user name are affected.
- **`default_entity_id` pins the id** on both versions (scenario 5), and HA accepts it alongside
  `object_id`.

Neither 2026.8.1 (the declared minimum) nor current stable was in that run, which is why row 2
gates the merge.

### Why the name-derived id and not `object_id`

For 10 r5 entities (11 counting the tracker) the `object_id` differs from the id HA derives from
the names, and the dashboards and docs use the name-derived form. For the a290 every `object_id`
differs, because the device is named "Alpine A290" and the prefix is `a290_`.

### The slug rule, and a correction to the RFC

HA's `homeassistant.util.slugify(text, separator="_")` returns `""` for empty input, `"unknown"`
when the slug comes out empty, and otherwise `python-slugify`'s `slugify(text, separator="_")`.
HA pins `python-slugify==9.0.0`. For ASCII text that version: lower-cases; drops a comma between
two digits (`1,000` becomes `1000`); turns every other run of characters outside `[a-z0-9]` into
one separator; strips separators from both ends.

The RFC said apostrophes are dropped. **For the ASCII apostrophe that is wrong:** python-slugify
replaces `'` with the separator before anything else, so "Driver's Door" slugs to
`driver_s_door`. Only a non-ASCII apostrophe such as `’` is dropped, after transliteration.

Two inputs are outside what the core can reproduce, so `configure()` rejects them rather than
publish an id that silently differs from HA's:

- **Non-ASCII.** HA transliterates with whichever of Unidecode or text-unidecode is installed,
  and the two can differ. Neither real catalog has a non-ASCII entity or device name; the
  non-ASCII in them is only in units (`°C`) and comments.
- **HTML character references** (`&amp;`, `&#39;`, `&#x26;`), which python-slugify decodes first.

A name whose combined slug would be empty is rejected too; HA would call it `unknown`.

## Alternatives

Copied verbatim from RFC 0005.

| Option | What | Cost | Risk |
| --- | --- | --- | --- |
| **A (recommended)** | Core sends `default_entity_id` = the name-derived id; keeps `object_id` | One core release; a `CORE_REF` bump and patch release in each add-on; one core test; the conformance test reads the new field | Low. Plain installs unchanged; the slug rule is pinned by a test. Existing prefixed entities are not repaired |
| B | Core sends `default_entity_id` = the `object_id` | As A | **High.** New installs get 10 ids that no dashboard or doc uses, so the dashboards would need two id sets or would break for existing users |
| C | No code change; document the cause and the manual rename in DOCS.md troubleshooting | Docs only, both repos | Recurs: any user whose car device has an area hits it again with every entity a future release adds |
| D | A, plus the add-on renames existing prefixed entities back over HA's websocket at startup | High: a registry-writing migration, its own tests, and container verification against real HA | Rewrites a user's own registry and could undo a deliberate choice. That is live state changed to make a display look right, which the house rules forbid |

## Consequences

**Accepted**

- Existing prefixed entities are not repaired (scenario 4). Affected users rename by hand, as
  the #83 reply describes.
- These entities opt out of HA's area-based naming. A user who wants an area prefix renames
  the entities themselves.
- An entity's name is now part of its id contract. Renaming an entity in a catalog changes the
  id a new install gets, so a rename is a breaking change for dashboards.
- An add-on with a non-ASCII or `&...;` name now fails at startup instead of publishing.
- An add-on's byte-identical discovery diff (docs/adding-a-model.md §6) is no longer empty
  across this core bump: every config gains one key.
- An id already taken, for example by an orphaned entity, still gets HA's `_2` suffix.

**Open items**

- **Untested:** whether re-discovery *with* `default_entity_id` renames an already-registered
  prefixed entity. HA's code (`_init_entity_registry` in `homeassistant/components/mqtt/entity.py`)
  suggests it does so only for a deleted registry entry.
- **The tombstone-while-HA-offline gap:** a separate RFC is coming.

## Verification (2026-09-26)

- **The unit tests fail without the change.** Run against unmodified `mqtt.py`, the new tests
  gave 21 failures: 13 `KeyError: 'default_entity_id'` and 8 `DID NOT RAISE ValueError`.
- **The slug tests discriminate.** A mutant `_slug` that drops apostrophes and has no
  digit-comma rule fails 2 of them.
- **The helper agrees with python-slugify 9.0.0.** 199,739 random printable-ASCII strings
  without character references, and every emitted config for the real a290 (57) and r5 (58)
  catalogs driven through `publish_discovery`, gave 0 mismatches.
- **What this cannot establish:** that HA itself derives these ids. The comparison is with the
  library HA pins, not with a running HA; the registry path, area prefixing and `_2` collisions
  are not exercised. Row 2 is the check that settles that.

## References

- RFC 0005: pin MQTT entity ids with default_entity_id:
  https://claude.ai/code/artifact/e4bf159f-2c74-4efd-a64c-aa29e68c914f
- r5-ha-addon issue #83: https://github.com/MatthewHobbs/r5-ha-addon/issues/83
- HA `homeassistant/util/__init__.py` `slugify`, read at dev `cbda41f` (2026-09-26);
  last commit touching the file `eae809a`.
- HA `homeassistant/components/mqtt/entity.py` `_init_entity_registry`, read at dev `cbda41f`.
- python-slugify `slugify/slugify.py` at tag v9.0.0 (`b6ccd59`).
