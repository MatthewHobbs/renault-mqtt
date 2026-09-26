# ADR 0002 — Remove entities whose tombstone Home Assistant missed

- **Status:** Accepted 2026-09-26. I accepted option C2 once the gate measurements were recorded
  and the e2e harness confirmed it on both declared Home Assistant versions.
- **Context:** the core retires an entity by publishing an empty retained payload to its discovery
  topic. If Home Assistant is not running at that moment, it never sees the tombstone and the
  entity stays `unavailable` in the registry indefinitely. Proposed as
  [RFC 0009: remove entities whose tombstone Home Assistant missed](https://claude.ai/code/artifact/bd0ea77a-03ad-49cb-9a96-fdb47a95531a).
- **North star:** an entity the core has retired does not linger as a permanent orphan just
  because Home Assistant happened to be down at the moment of retirement.

<!-- Format: claude-config docs/adr/0000-template.md, referenced at source rather than copied.
The Decision table is the plan and is kept current; the rest is the dated record. -->

## Decision

Option **C2**: on Home Assistant's birth message (`homeassistant/status` = `online`), the core
re-publishes discovery from its stored context — in addition to its existing re-publish on its
own broker reconnect. Every retirement (the `RETIRED_*` lists, and endpoints the car does not
support), across all four domains, is sent as an **unretained minimal config** — `unique_id`,
`device`, `enabled_by_default: false`, and the domain's one required topic — immediately followed
by the ordinary empty retained payload, instead of the empty payload alone. The device_tracker's
location opt-out (`PUBLISH_LOCATION=false`) is retired the same way, keyed on
`json_attributes_topic` rather than `state_topic`/`command_topic`, since it is also a retirement
in the RFC's sense (dual review, row 2 below).

Decisions I confirmed on 2026-09-26 (RFC 0009's decision log):

1. **Option C2**, not plain C: the gate found plain C writes 2 recorder-history rows per id per
   cycle for an id the install never had, and cannot remove an orphan the install has disabled.
   C2 — the same sequence with `enabled_by_default: false` added to the minimal config — measured
   no history or logbook writes on either HA version, and is the only variant that also removes a
   disabled orphan.
2. **The gate (Phase 1) is approved** and its results are recorded below.
3. **r5-ha-addon's object_id renames do not wait for this fix.** They proceed after 0.18.0, per
   ADR 0001 row 5.

| # | Step | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Gate: four measurements (M1–M4) on HA 2026.8.1 and 2026.9.3, comparing plain C against C2 | renault-mqtt (e2e harness) | Done | 2026-09-26, branch `fix-offline-tombstones` `eb6e060`: see "Gate results" below |
| 2 | `mqtt.py`: subscribe to `homeassistant/status`, re-publish discovery on `online`; every retirement sent as C2's minimal config then the empty payload; unit tests written first and shown to fail | renault-mqtt | Done | `a214673` (failing tests), `8cd6853` (implementation) |
| 3 | End-to-end confirmation on both declared HA versions | renault-mqtt (e2e harness) | Done | 2026-09-26, HA 2026.8.1 and 2026.9.3, core `37f8114`: see "End-to-end results" below |
| 4 | Dual review (this repo is Public: Claude + Codex, reconciled) | renault-mqtt | Done | 2026-09-26: see "Dual review" below |
| 5 | Core release: `__version__` bump in its own PR; the release workflow tags it | renault-mqtt | Open | |
| 6 | a290-ha-addon: `CORE_REF` bump, patch release, container boot | a290-ha-addon | Open | |
| 7 | r5-ha-addon: the same, after row 6 | r5-ha-addon | Open | |

Status is one of **Open**, **Done**, **Blocked**, **Dropped**. A **Done** row carries Evidence.

## Context (2026-09-26)

Read from Home Assistant's source (`components/mqtt/discovery.py`, identical at 2026.8.1 and
2026.9.3):

- An empty retained publish **deletes** the broker's retained config, so a restarted HA receives
  nothing for that topic.
- HA acts on an empty discovery payload only for an entity it has discovered **in the current
  run** (`already_discovered`, discovery.py:535; the other branch at :578–598 does nothing unless
  the entity is disabled).
- The add-on re-sent discovery only on its own MQTT reconnect. It did not listen to HA's birth
  message on `homeassistant/status`, which HA's MQTT docs tell discovery publishers to use as the
  trigger. That message is not retained, and is sent about 5 s after HA has processed retained
  discovery, so it is not by itself a substitute for the add-on's own re-publish-on-connect: a
  subscriber that joins the broker late needs that path instead.

This already applied to `RETIRED_SENSORS`, and 0.18.0 extended retirement to binary sensors,
buttons and numbers ahead of r5's 14 `object_id` renames (ADR 0001 row 5), which increases how
often an install can hit the gap.

### Gate results (2026-09-26)

Measured with the e2e harness on HA 2026.8.1 and 2026.9.3, with identical results on both. Plain
C's minimal config carried only `unique_id`, `device` and the domain's required topic; C2 adds
`enabled_by_default: false`.

| Measurement | C as written (minimal config) | C2 (+ `enabled_by_default: false`) |
| --- | --- | --- |
| M1 history, ids never registered | **2 recorder rows per id per cycle** (`unknown`, then `""`); 24 rows in the test | none |
| M1 logbook | none | none |
| M1: a later real entity with that id | gets its intended id | gets its intended id, enabled |
| M2: orphan that is disabled by default or by the user | **not removed** | removed |
| M2: enabled orphan | removed | removed |
| M3: 20 back-to-back trials per class per version | 20/20 (one at a time and as a burst) | 20/20 (one at a time and as a burst) |
| M4: reboot with the add-on publishing before HA, broker persistence on | pre-fix core: orphan not removed after 180 s | 137/137 rows as expected (see below) — including `M4`/`d2` |

Churn: each core pass cycles 11 ids on a290 and 8 on r5. HA sends its birth message on **every**
reconnect to the broker, not only at its own start, so the cycles repeat with each one. Option D
would not avoid this either: it still cycles the retired ids an install never had.

**Not established:** whether the recorder's purge (10 days by default) clears the M1 rows; any
effect on long-term statistics; a real Supervisor host reboot (the harness's `d2`/M4 reproduces the
gap by stopping HA and the add-on, not by rebooting the host).

### A finding the RFC did not anticipate: `number` needs `command_topic`, not `state_topic`

The e2e harness's first run against the implementation (HA 2026.9.3) gave 136/138 rows as
expected: `d2` and `d3` were `NOT-REMOVED` for the `number` domain only, while `sensor`,
`binary_sensor` and `button` all removed cleanly. Home Assistant's `mqtt.number` schema makes
`command_topic` mandatory; `state_topic` is optional. A minimal config carrying only
`state_topic` therefore fails HA's schema validation, so HA never discovers the id and the empty
payload that follows tombstones nothing. Unit tests against a `StubClient` cannot see this — they
check what the core publishes, not what a real HA does with it, which is exactly what the e2e
harness's scenarios are for. Fixed in `37f8114` by sending `command_topic` for both `button` and
`number`'s minimal configs.

## Alternatives

Copied verbatim from RFC 0009, plus C2, added to the RFC after the gate.

| Option | What | Cost | Risk |
| --- | --- | --- | --- |
| A | Status quo; document "delete the entity from its settings dialog" in both add-ons' DOCS.md | Docs only, both repos | Every user who hits the race keeps orphans until they act. The device page's Delete must not be recommended: it also wipes the retained configs of the device's live entities |
| B | Re-publish discovery on the birth message only | Small: one subscription and one handler | Does **not** fix this (measured). Still worthwhile on its own: it recovers a broker that lost its retained configs |
| C | B, plus every retirement sent as an unretained minimal config followed by the empty retained payload | Moderate: a minimal config per domain for retired and unsupported ids; unit tests; new harness scenarios | On every HA start, HA briefly creates and removes an entry for each retired id, including ids this install never had. Measured: 2 recorder history rows per id per cycle, and cannot remove a disabled orphan |
| **C2 (adopted)** | C, with `enabled_by_default: false` added to the minimal config | As C | No history or logbook writes measured, and the only variant that also removes disabled orphans. A deleted-entity record is still kept per churned id; it did not change later ids, because the core always sends `default_entity_id` |
| D | C, but only for ids in the `RETIRED_*` lists, not for unsupported endpoints | As C, slightly less | Less churn; orphans from a car losing an endpoint, or from the refresh-location button being switched off, remain |
| E | The add-on removes orphans itself through HA's registry API (Supervisor token) | High: a new privilege, a websocket client, API-version coupling | The add-on writes to the user's registry, which the house rules treat as mutating live state. Listed for completeness |

"Wait for the normal mechanism" is not an option here: HA has no mechanism that removes such an
orphan on its own. It stays `unavailable` until the user deletes it.

## Consequences

**Accepted**

- Every HA start or broker reconnect briefly creates and removes a disabled registry entry for
  every retired or unsupported id, including ids a given install never had. This is by design
  under C2, not a defect: it is what lets the tombstone reach an id HA's current run never
  discovered.
- A deleted-entity record accumulates per churned id across restarts. It does not change the id a
  later real entity gets, because the core always sends `default_entity_id`.
- Recorder/logbook impact from the churn was not measured beyond the gate's single-run snapshot;
  purge behaviour and long-term statistics are unestablished.
- A real Supervisor host reboot was not exercised; the harness reproduces the gap by stopping HA
  and the add-on, not by rebooting the host.
- Both add-ons pick this up by bumping `CORE_REF`, with no catalog change. The same sequence also
  cleans up orphans that past releases have already left behind, provided their ids are still
  listed in the relevant `RETIRED_*` table.

**Open items**

- A user who has changed HA's birth **topic or payload**, or disabled the birth message entirely
  (all three are supported HA configuration), gets the add-on's own reconnect re-publish only, not
  the birth-triggered one. This was accepted as a risk in RFC 0009 itself ("Other risks"), not
  discovered by this ADR's review; adding configurability for it is a separate decision, not a fix
  to this one.
- HA 2026.7.1 and earlier were not tested; the add-ons' declared minimum is 2026.8.1.
- **The device_tracker's location opt-out uses the same C2 sequence by code and unit test only.**
  Unlike the four `_RETIRABLE` domains, it was not run through the e2e harness's offline-tombstone
  scenarios (`d2`/`d3`), which only cover `CLASSES = (sensor, binary_sensor, button, number)`.
  `json_attributes_topic` is the field HA's device_tracker schema needs (confirmed by the live
  tracker config, which already relies on it alone), so the mechanism is expected to work the same
  way `command_topic` does for button/number, but that expectation is not gate-measured the way
  the other four are. A dedicated harness scenario is the natural follow-up if this needs the same
  standard of evidence the other domains got.

## Verification (2026-09-26)

- **The unit tests fail without the change.** `tests/test_mqtt.py`, run against the pre-fix
  `mqtt.py` (`a214673`), gave 7 failures: no subscription to `homeassistant/status`, no
  republish-on-birth handling, and every retirement writing only the final empty payload instead
  of the minimal-config-then-tombstone pair.
- **`just ci` is green** after the implementation: 145 tests, 100% coverage, lint clean.
- **What unit tests cannot establish:** whether Home Assistant actually discovers and removes the
  minimal config — exactly the question the `number`/`command_topic` finding above turned on.

### End-to-end results

Harness `tests/e2e` (`just e2e`), branch `fix-offline-tombstones` at `37f8114`: Mosquitto plus a
fresh HA container, the MQTT integration set up through its config flow, the core's own recorded
publishes replayed in order, and ids/registry state read over HA's websocket API. Real catalogs:
`alpine_a290` and `renault_5`.

| HA version | Result |
| --- | --- |
| 2026.9.3 | 137/137 rows as expected (after the `number`/`command_topic` fix; 136/138 before it, with `d2` and `d3` `number` the only unexpected rows) |
| 2026.8.1 | 137/137 rows as expected |

`d2` (a broker restart with persistence on, the core's own client retiring one entity per class
before HA starts) and `d3` (HA offline for the tombstone, then the same pass re-sent once HA's
birth message arrives) both showed every domain — sensor, binary_sensor, button, number —
`REMOVED` on both HA versions, which is the check the gate said must flip for C2 to be accepted.

## Dual review (2026-09-26)

This repo is Public, so the one review rule ran as Claude's own pass plus Codex's (`just
review-diff`), reconciled. Two passes, since the first fix changed the head SHA and the rule
requires reviewing the SHA that merges:

1. **First pass**, against `8b73b54`: Codex found the e2e harness's status watcher was armed only
   when `d5` was explicitly named, so a `--scenarios d3` or `--scenarios d4` run — which still
   reports `d5`'s rows, since they share one HA restart — would false-fail on `d5/first-setup`.
   My own pass agreed: `git diff` confirmed this condition was introduced by this branch's own
   commits, not pre-existing. **Fixed** in `ee5a4ab` and verified directly: `--scenarios d3` on
   HA 2026.9.3 gave 27/27 rows as expected, `d5/first-setup: SEEN`.
2. **Second pass**, against the fixed head: Codex found the device_tracker's location opt-out
   (`PUBLISH_LOCATION=false`) still tombstoned with a plain empty payload, missing the exact fix
   this ADR is about, for the one entity every install has by default. My own pass agreed — it is
   a retirement in the RFC's sense, just not one of the four `_RETIRABLE` domains the gate
   measured. **Fixed**: the opt-out path now uses the same `_tombstone()` helper, keyed on
   `json_attributes_topic`. Not run through the e2e harness (see Open items above) — the fix and
   its unit test rest on the same mechanism the other four domains already proved, not on a fresh
   measurement of this specific path.

3. **Third pass**, against the fixed head: Codex found the birth-message subscription is hard-coded
   to the default topic and payload, and HA supports customizing (or disabling) both, so such a
   user only gets the add-on's own reconnect recovery, not the birth-triggered one. This is real,
   but it is not a new defect: RFC 0009's own "Other risks" already named the topic case as an
   accepted limitation. **Dismissed as a new finding, but the ADR's wording was tightened** to
   cover payload customization too, which the RFC's phrasing had missed. Adding configurability
   for a non-default birth topic/payload is a separate decision, not a fix to this one.

The first two findings were in code this branch itself introduced, not pre-existing debt, and were
fixed and re-verified. The third restates a risk already accepted at the RFC stage.

## References

- RFC 0009: remove entities whose tombstone Home Assistant missed:
  https://claude.ai/code/artifact/bd0ea77a-03ad-49cb-9a96-fdb47a95531a
- ADR 0001: pin entity ids with default_entity_id (row 5: r5's rename order)
- HA `homeassistant/components/mqtt/discovery.py`, read at 2026.8.1 and 2026.9.3 (identical).
- HA `homeassistant/components/mqtt/number.py` schema (`command_topic` required, `state_topic`
  optional), read 2026-09-26.
