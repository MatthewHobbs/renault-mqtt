# ADR 0003 — Normalise bashio's "null" at the add-on entrypoint, not in core

- **Status:** Proposed 2026-10-29.
- **Context:** `a290-ha-addon` 1.30.2 shipped a regression (#309) where leaving the optional
  `precondition_temperature` unset caused Start Climate to fail with
  `ValueError: could not convert string to float: 'null'`, and clearing `account_id` passed `"null"`
  as the Kamereon account id and masked the substring `"null"` across logs. Fixed for those two keys
  in `run.sh` in `a290-ha-addon` 1.30.3 (#309, merge `12ae0e3`) and `r5-ha-addon` 1.10.0 (#209,
  merge `10273da6139908974e7203c0b8914ed582ce2fb5`). Proposed as RFC 0001: handling bashio's
  `"null"` output for unset optional settings in `renault-mqtt`.
- **North star:** every optional (`?`) setting declared in an add-on's `config.yaml` `schema:` is
  exported by `run.sh` through `opt()` so that an unset or cleared option reaches every Python
  reader in the container (`renault_mqtt`, `main.py`, and `deploy.py`) as the empty string `""`,
  never as bashio's four-character sentinel `"null"`, and a structural test in each add-on fails CI
  if any `?` key is ever exported with raw `bashio::config`.

<!-- The Decision table is the plan and is kept current; the rest is the dated record. -->

## Decision

Option **1** of RFC 0001: keep `"null"` normalisation in each add-on's `run.sh` entrypoint (`opt()`),
enforce it mechanically across every `?` key in `config.yaml` `schema:` with a structural test in
both add-ons, and leave `renault_mqtt` unchanged.

Decisions I confirmed on 2026-10-29:

1. **Option 1** of RFC 0001 (see Alternatives). `bashio::config`'s `"null"` output for omitted JSON
   keys is a quirk of the shell entrypoint library, so the shell entrypoint (`run.sh` `opt()`) is
   the boundary that normalises it.
2. **Mechanical boundary enforcement in both add-ons.** Every key in `config.yaml` `schema:` whose
   type ends with `?` must be exported in `run.sh` via `opt '<key>'`, never raw `bashio::config`. A
   structural test in each add-on (`test_main.py`) reads `config.yaml` `schema:`, collects all `?`
   keys, and asserts both statically (`run.sh` exports each via `opt` and none via raw
   `bashio::config`) and dynamically (running `run.sh` with `{}` in `OPTIONS_JSON` under
   `bashio::config`'s `${2:-null}` implementation exports `""` for every `?` key).
3. **Wrap the five `charger_*` (`str?`) keys in `opt()` in `run.sh`, and keep `_charger_eid`'s
   `eid.lower() != "null"` check in `deploy.py` as defence in depth.** Wrapping `charger_*` in
   `opt()` in `alpine_a290/run.sh:55-59` and `renault_5/run.sh:56-60` eliminates the exception to the
   boundary rule so the structural test holds with zero carve-outs and every Python reader sees `""`.
   `_charger_eid` (`alpine_a290/app/deploy.py:147-150`, `renault_5/app/deploy.py:174-177`) keeps its
   existing `eid.lower() != "null"` guard as zero-cost defence in depth because `deploy.py` is also
   imported standalone by `ui-tests/seed.py` outside `run.sh`.
4. **Why `renault_mqtt` keeps two models (`_opt_flag` vs `cfg`), the cost accepted, and the trigger
   to revisit.** In `renault_mqtt/config.py`, `_opt_flag` (`config.py:35-41`) tolerates `"null"` for
   boolean flags, whereas `cfg` (`config.py:31-32`) is a raw `os.environ.get(name, default)` lookup
   that returns `"null"` verbatim if `"null"` is in the environment. I am leaving both as they are:
   changing `cfg` or `_opt_flag` requires a core version bump, signed tag, and `CORE_REF` bumps in
   both add-ons while changing no runtime behaviour on any install and still leaving non-core
   `os.environ.get` readers (`deploy._redact`, `deploy.run_deploy`, `main.py` `GPS_PRECISION`)
   dependent on `run.sh`. The accepted cost is that `config.py` retains two models of the
   environment and any non-`run.sh` caller that sets `<PREFIX>ACCOUNT_ID="null"` or
   `<PREFIX>PRECONDITION_TEMPERATURE="null"` directly in `os.environ` is not normalised by `cfg()`.
   **Trigger to revisit:** if `renault-mqtt` ever gains a non-`run.sh` entrypoint, or if a second
   option-reading helper is added or touched in `renault_mqtt/config.py`, unify `config.py` in that
   core change.
5. **No core release and no add-on release for this work.** In `renault-mqtt`, only documentation
   (`docs/adr/` and `docs/adding-a-model.md`) changes. In `a290-ha-addon` and `r5-ha-addon`,
   wrapping `charger_*` in `opt()` and adding the structural test does not change user-visible
   behaviour on any install (`_charger_eid` already treated `"null"` as `None`), so under the
   add-ons' release and `docs_sync_check.py` rules it needs **no `CHANGELOG.md` entry, no
   `config.yaml` `version` bump, and no release tag**; the `run.sh` change ships in the next normal
   add-on release.

| # | Step | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| 1 | `a290-ha-addon`: normalise `"null"` to `""` via `opt()` in `run.sh` for `account_id` and `precondition_temperature`; pin in `test_main.py` | a290-ha-addon | Done | [PR #309](https://github.com/MatthewHobbs/a290-ha-addon/pull/309) (branch `4643c55`, merge `12ae0e3`), shipped in v1.30.3 |
| 2 | `r5-ha-addon`: the same in `renault_5/run.sh` and `renault_5/tests/test_main.py` | r5-ha-addon | Done | [PR #209](https://github.com/MatthewHobbs/r5-ha-addon/pull/209) (branch `50109a0`, merge `10273da6139908974e7203c0b8914ed582ce2fb5`), shipped in v1.10.0 |
| 3 | `a290-ha-addon`: wrap `charger_*` in `opt()` in `alpine_a290/run.sh` and enforce with a structural test in `alpine_a290/tests/test_main.py` covering every `?` key in `alpine_a290/config.yaml` `schema:` | a290-ha-addon | Open | |
| 4 | `r5-ha-addon`: mirror row 3 in `renault_5/run.sh` and `renault_5/tests/test_main.py` | r5-ha-addon | Open | |
| 5 | Document `opt()` and the structural test requirement for optional (`?`) schema keys in `docs/adding-a-model.md` | renault-mqtt | Open | |

Status is one of **Open**, **Done**, **Blocked**, **Dropped**. A **Done** row carries Evidence.

## Context (2026-10-29)

### Why `bashio::config` prints `"null"` and which schema keys can trigger it

Read from `hassio-addons/bashio` (`lib/config.sh:16-56`) and `home-assistant/supervisor`
(`supervisor/apps/options.py:128-187, 298-315`), and reproduced against the published
`ghcr.io/matthewhobbs/alpine_a290:1.30.3` image:

- `bashio::config` sets `local default_value=${2:-null}`. Because bash's `:-` substitutes `null`
  when `$2` is unset **or** `""`, `bashio::config '<key>'` and `bashio::config '<key>' ''` both echo
  `"null"` whenever `jq` returns `"null"`.
- In `bashio::config`'s `jq` query, a JSON string (`type == "string"`) evaluates `.${key} // empty`,
  so an empty string `""` in `/data/options.json` outputs `""`, **not** `"null"`. Only a key that is
  absent or `null` in `/data/options.json` (or literally set to the string `"null"`) causes `jq` to
  output `"null"`.
- In the current Supervisor source, `AppOptions` rejects `None` (`null`) for every present key and
  rejects missing keys unless the schema type ends with `?` (`options.py:131-134, 307-314`).

Across `alpine_a290/config.yaml` and `renault_5/config.yaml`, that divides the schema into two
groups under the current Supervisor validator:

| Group | Keys | Schema type | Can `/data/options.json` omit it under current Supervisor validation? | What `bashio::config` outputs when unset |
| --- | --- | --- | --- | --- |
| Required keys with defaults in `options:` | `username`, `password`, `vin`, `locale`, `poll_interval`, `battery_capacity_kwh`, `stale_hours`, `publish_location`, `enable_refresh_location`, `gps_precision`, `log_level`, `debug_dump`, `deploy_dashboard`, `dashboard_url_path`, `redeploy_dashboard`, `car_render` (`r5`) | `str`, `password`, `int(...)`, `bool`, `list(...)` (none with `?`) | **No** under current `AppOptions` (`options.py:298-315`); `str`/`password` default to `""` in `options:` | Blank `str`/`password` keys hold `""` in `/data/options.json`, for which `bashio::config` outputs `""`. |
| Optional keys (`?`) | `account_id`, `precondition_temperature`, `charger_smart_charge`, `charger_bump_charge`, `charger_target_soc`, `charger_target_time`, `charger_dispatching` | `str?`, `float(16,27)?` | **Yes**, when unset or cleared in the UI | `"null"` from raw `bashio::config`; `""` when wrapped in `opt()` in `run.sh` (rows 1–4). |

### Correction to RFC 0001

RFC 0001 §1.2 and §2 Option 1(b) stated that `username`, `password`, `vin`, `poll_interval`,
`battery_capacity_kwh` and `stale_hours` were exposed to `"null"` under normal operation. Under the
current Supervisor schema validator and `config.yaml`, none of those keys has `?`, and blank
`username`/`password`/`vin` values in `/data/options.json` are `""`, which `bashio::config` exports
as `""` (whether an older Supervisor release ever wrote an absent or `null` required key on upgrade
was not tested and remains an open item below).

RFC 0001 §3 also raised the question of whether normalising `"null"` in `cfg()` (Shape A) would
break a literal password of `"null"`. Because `bashio::config` itself checks
`if [[ "${result}" == "null" ]]` after `jq --raw-output`, an unset key and a literal string `"null"`
are indistinguishable at the `bashio` boundary; and because `password` is a required key whose
default is `""`, `bashio::config 'password'` outputs `""` when blank.

### Why `run.sh` is the right boundary, and why `charger_*` are wrapped too

Add-on code outside `renault_mqtt` reads environment variables directly with `os.environ.get`
because `deploy.py` does not import `renault_mqtt` (`ui-tests/seed.py` imports `deploy` without the
core installed). Hardening `renault_mqtt.config.cfg()` therefore cannot protect `deploy.py` (such as
`deploy._redact` reading `<PREFIX>ACCOUNT_ID`, or `_charger_eid` reading `<PREFIX>CHARGER_*`) or
`main.py`'s `GPS_PRECISION`.

In `a290-ha-addon` 1.30.3 and `r5-ha-addon` 1.10.0, `account_id` and `precondition_temperature`
were wrapped in `opt()`, but the five `charger_*` (`str?`) keys in `alpine_a290/run.sh:55-59` and
`renault_5/run.sh:56-60` were left on raw `bashio::config`, relying on `_charger_eid`'s
`eid.lower() != "null"` guard in `deploy.py`. Wrapping `charger_*` in `opt()` makes the `run.sh`
rule uniform — every `?` key in `config.yaml` `schema:` goes through `opt()` — so a single
structural test in each add-on can enforce the boundary without exceptions, while `_charger_eid`
retains `eid.lower() != "null"` as defence in depth.

## Alternatives

Adapted from RFC 0001, corrected for Supervisor schema validation:

| Option | What | Cost | Risk |
| --- | --- | --- | --- |
| **1 (adopted)** | Keep `renault-mqtt` unchanged; `run.sh` normalises `"null"` to `""` via `opt()` on every `?` schema key (`account_id`, `precondition_temperature`, and the five `charger_*` keys); enforce with a structural test in each add-on; document in `docs/adding-a-model.md` | Docs only in `renault-mqtt`; small `run.sh` + test PR in each add-on; no core or add-on release needed | `_opt_flag` and `cfg` in `renault_mqtt/config.py` retain different `"null"` handling; non-`run.sh` callers of `cfg()` must not pass `"null"` for unset values |
| 2 | Harden `renault-mqtt` (`cfg()` or individual readers) and drop `opt()` in `run.sh` | Core PR + release + `CORE_REF` and `run.sh` PRs in both add-ons | **Breaks `deploy._redact`**, which reads `<PREFIX>ACCOUNT_ID` via `os.environ.get` directly and would mask `"null"` in deploy error logs if `opt('account_id')` were removed from `run.sh` |
| 3 | Keep `opt()` in `run.sh` **and** harden `renault_mqtt.config.cfg()` (Shape A) or individual core readers (Shape B) | Core PR + release + unit tests + `CORE_REF` bumps in both add-ons | Ships no user-visible behaviour change; duplicates `"null"` handling in two layers while `deploy.py` still relies on `run.sh` |

## Consequences

**Accepted**

- `renault_mqtt.config.cfg()` remains a plain `os.environ.get(name, default)` wrapper, while
  `_opt_flag()` in the same module keeps its `("", "null")` check for boolean flags. We accept the
  two models in `renault_mqtt/config.py` to avoid an unnecessary core release and `CORE_REF` churn,
  and will revisit if `renault-mqtt` ever gains a non-`run.sh` entrypoint or `config.py`'s option
  helpers are next modified.
- Every optional (`?`) setting in an add-on's `config.yaml` `schema:` must be exported in `run.sh`
  via `opt '<key>'` rather than raw `bashio::config '<key>'`, enforced by a structural test in each
  add-on.
- `_charger_eid` in `deploy.py` keeps `eid.lower() != "null"` as defence in depth alongside
  `opt 'charger_*'` in `run.sh`.
- Neither a core release nor an add-on release is cut for rows 3–5: they change no user-visible
  behaviour on any install, require no `CHANGELOG.md` entry under the add-ons' `docs_sync_check.py`
  rules, and ride the next normal release of each add-on.

**Open items**

- **Historical Supervisor upgrades:** whether an older Home Assistant Supervisor version ever wrote
  an absent or `null` non-`?` required key into `/data/options.json` across an add-on upgrade was
  not established (`supervisor/apps/app.py:454` merges `self.data["options"]` before
  `self.persist["options"]` in current Supervisor, but older Supervisor releases were not tested in
  a container).

## Verification (2026-10-29)

- **The structural `opt()` check fails on current `run.sh` and on a synthetic raw `?` key, and
  passes once `charger_*` use `opt()`.** Run against `alpine_a290/config.yaml` and
  `alpine_a290/run.sh` from `a290-ha-addon` `7552a43` (and `renault_5/config.yaml` and
  `renault_5/run.sh` from `r5-ha-addon` `d8c832a`), the structural test fails with
  `AssertionError: optional schema keys exported with raw bashio::config: ['charger_bump_charge', 'charger_dispatching', 'charger_smart_charge', 'charger_target_soc', 'charger_target_time']`.
  With those five lines switched to `opt 'charger_*'`, both add-ons pass; adding a synthetic
  `synthetic_opt: str?` to `config.yaml` exported with `bashio::config 'synthetic_opt'` fails the
  test naming `synthetic_opt`.
- **Core test suite and linter pass:** `python3 -m pytest -q && ruff check .` in `renault-mqtt` at
  `970c55a78d71dd39aef57b763845ef54a32ea14f` (and on this branch at `4de697f`): `348 passed`,
  `All checks passed!`.

## References

- RFC 0001: handling bashio's `"null"` output for unset optional settings in `renault-mqtt` (PR #40).
- `a290-ha-addon` PR #309: https://github.com/MatthewHobbs/a290-ha-addon/pull/309 (branch `4643c55`,
  merge `12ae0e3`).
- `r5-ha-addon` PR #209: https://github.com/MatthewHobbs/r5-ha-addon/pull/209 (branch `50109a0`,
  merge `10273da6139908974e7203c0b8914ed582ce2fb5`).
- `hassio-addons/bashio` `lib/config.sh` (`bashio::config`), read 2026-10-29.
- `home-assistant/supervisor` `supervisor/apps/options.py` (`AppOptions`), read 2026-10-29.
