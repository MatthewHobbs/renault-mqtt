# End-to-end Home Assistant harness

Shows what a real Home Assistant does with this core's MQTT discovery output: which entity ids
it assigns, and whether tombstones remove entities. Unit tests can only check what the core
*publishes*. These questions are about what HA *does* with it, and they are what changes to
entity naming and retirement have to show before they merge.

It is **not** part of `just ci`. It needs Docker, takes several minutes per HA version, and
nothing under `tests/e2e/` is collected by `pytest tests` (no file is named `test_*.py`).

## Running it

```sh
just e2e 2026.9.3
just e2e 2026.8.1 /path/to/core-checkout --catalog /path/to/alpine_a290/app:catalog --catalog /path/to/renault_5/app:catalog --out /tmp/e2e.json
```

Arguments: the HA version (a tag of `ghcr.io/home-assistant/home-assistant`), the core checkout to
import `renault_mqtt` from (defaults to this repo), and, for scenario (f), each real add-on catalog
as `SYS_PATH_ENTRY:MODULE`. `--scenarios a,b,c,g,d1,d2,e,f` picks a subset. `--e-repeats N` runs
the one-pass rename N times on fresh devices: it is a race between a tombstone and a config sent
microseconds apart, so one passing trial could be luck. The harness refuses
to run if `renault_mqtt` was imported from anywhere other than the path given.

Test at both ends of what the add-ons claim: current stable
(`https://version.home-assistant.io/stable.json`) and the oldest version they declare
(`homeassistant:` in each add-on's `config.yaml`).

The stack is `rmqtt-e2e-<http port>-net`, `-mqtt` (Mosquitto on 127.0.0.1:18831) and `-ha`
(HA on 127.0.0.1:18131), torn down on exit, failure or SIGTERM. Use `--keep` only to debug.

## What makes the results evidence

- **Payloads are the core's real output.** `publish_discovery` runs against a recording client and
  the recording is replayed in order, empty payloads included, with the core's own retain and QoS.
  The only routine edit is per-scenario isolation: a suffix on the topic node, `unique_id` and
  device identifier. Every other departure is labelled in the result (`edited`,
  `tombstone_source`, `default_entity_id_source`).
- **Registries are read through HA's websocket API**, never by writing to them in-process. An
  `entity_registry.async_get_or_create` shortcut was tried and is unfaithful: it drops the entity
  name, which is what the entity id is derived from.
- **Every scenario device has its own name.** If they shared one, later scenarios would collide
  and gain `_2` suffixes that say nothing about naming.
- **Each negative has a positive control** that the same instrument can see the positive:
  - (d1) is the control for (d2).
  - `e/control` must produce `X_2`, or the harness could not detect a collision at all.
  - (g) only counts if HA logged `Updating component: <id>` for that entity.
  - (f) removes `default_entity_id` before publishing. Once it is in the payload HA uses it
    verbatim, so reading it back can never catch a wrong slug. Comparing HA's own derivation with
    the value the core would have sent can.

## Scenarios

| | Question |
|---|---|
| a | The same `unique_id` republished with a new entity name keeps its entity id? |
| b | A new `unique_id` without `default_entity_id`, on a device already in an area, gets an area-prefixed id (`garage_…`)? |
| c | The same, but with `default_entity_id`: is the id exactly `default_entity_id`? Needs a core that emits it. |
| d1 | HA online: does an empty retained payload remove the entry, for each of sensor, binary_sensor, button and number? |
| d2 | HA offline when the tombstone is published: is the entry removed after restart? |
| e | Rename with an unchanged friendly name. Tombstone the old `unique_id` and publish a new one with `default_entity_id` = the old id X: does it get X or `X_2`? Runs as one pass, as two passes (tombstone, wait for removal, publish), and as a no-tombstone control. |
| g | An entity already registered under a `garage_…` id is re-discovered with `default_entity_id`: renamed or left? Rechecked after the (d2) restart. |
| f | Plain install (no area) of each real catalog: the full HA-derived id list, and any mismatch with the core's `default_entity_id`. |

Scenarios the core under test cannot express are reported `N/A`, not skipped silently. A core
without `default_entity_id` gets it **injected** in (e) and labelled as simulated.

## Version trap: the MQTT config flow

2026.9.x sections the broker form and **requires**
`"other_settings": {"set_client_cert": false, "set_ca_cert": "off"}`. 2026.7.1 **rejects** that
key. The harness sends it only when `other_settings` appears in the returned `data_schema`. It also
POSTs `/api/onboarding/integration` during onboarding; without that, the MQTT flow returns no
`flow_id`.
