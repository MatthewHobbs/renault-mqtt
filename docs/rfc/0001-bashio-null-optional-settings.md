# RFC 0001 — Handling bashio's `"null"` output for unset optional settings in `renault-mqtt`

- **Status:** Resolved by
  [ADR 0003](../adr/0003-normalise-bashio-null-at-the-addon-entrypoint.md), accepted 2026-10-29.
  The owner chose Option 1: normalise `"null"` at each add-on's `run.sh` boundary with `opt()` and
  leave the core unchanged.
- **Context:** `a290-ha-addon` 1.30.2 shipped a regression where leaving `precondition_temperature`
  empty caused **Start Climate** to fail with `ValueError: could not convert string to float: 'null'`,
  and clearing `account_id` caused the add-on to use `"null"` as the Kamereon account ID and mask
  the substring `"null"` across logs (`a290-ha-addon` #309, fixed in `run.sh` in 1.30.3).
- **Question:** Should `renault-mqtt` treat the literal string `"null"` as unset for optional
  settings, keep normalisation in `run.sh` (`opt()`) only, or do both?

## 1. Verified mechanism and current state

### 1.1 Why `bashio::config` outputs `"null"`

In Home Assistant's base image (`/usr/lib/bashio/config.sh`), `bashio::config` assigns its default
with bash's `:-` operator:

```bash
local key=${1}
local default_value=${2:-null}
```

Because `${2:-null}` substitutes `null` whenever `$2` is unset **or empty** (`""`), both
`bashio::config 'precondition_temperature'` and `bashio::config 'precondition_temperature' ''`
write the four-character string `"null"` to stdout when the key is absent, `null`, or `""` in
`/data/options.json`.

Verified against the add-on image with `.orchestrate/repro-null.sh` and pinned in
`a290-ha-addon/alpine_a290/tests/test_main.py::test_run_sh_normalises_bashio_null_for_optional_settings`:

| `/data/options.json` | `bashio::config 'precondition_temperature' ''` | `opt 'precondition_temperature'` (`a290` 1.30.3) |
| --- | --- | --- |
| `{}` | `null` | *(empty)* |
| `{"precondition_temperature": null}` | `null` | *(empty)* |
| `{"precondition_temperature": ""}` | `null` | *(empty)* |
| `{"precondition_temperature": 22}` | `22` | `22` |

### 1.2 How `renault-mqtt` (`a163aef`, v0.21.0) currently reads environment variables

Option handling inside `renault-mqtt` is currently split between `_opt_flag` (which normalises
`"null"`) and `cfg` (which does not):

1. **`renault_mqtt/config.py:35-40` (`_opt_flag`):**
   ```python
   def _opt_flag(name, default):
       v = os.environ.get(name)
       if v is None or v.strip().lower() in ("", "null"):
           return default
       return v.strip().lower() in ("true", "1", "on")
   ```
   Used by `mqtt.configure()` for `<PREFIX>PUBLISH_LOCATION` and `<PREFIX>ENABLE_REFRESH_LOCATION`.

2. **`renault_mqtt/config.py:31-32` (`cfg`):**
   ```python
   def cfg(name, default=""):
       return os.environ.get(name, default)
   ```
   Returns `"null"` verbatim whenever the environment variable is `"null"`.

3. **Callers of `cfg` in `renault-mqtt` affected if `"null"` reaches the environment:**
   - `renault_mqtt/commands.py:28-34` (`precondition_temp`):
     `raw = (cfg(key, "") or "").strip(); return float(raw) if raw else None` — raises
     `ValueError: could not convert string to float: 'null'`.
   - `renault_mqtt/runtime.py:25-30` (`resolve_account`):
     `account_id = cfg_fn(f"{prefix}ACCOUNT_ID"); if account_id: return account_id` — returns
     `"null"`, skipping garage auto-discovery and requesting `.../accounts/null/vehicles/...`.
   - `renault_mqtt/config.py:55-58` (`_config_secrets`):
     collects `cfg(ENV_PREFIX + "VIN")`, `cfg(ENV_PREFIX + "ACCOUNT_ID")`,
     `cfg(ENV_PREFIX + "USERNAME")`, and `cfg(ENV_PREFIX + "PASSWORD")`. When any of those is
     `"null"`, `redact()` replaces every occurrence of `"null"` in log records and `/api/state`
     error strings with `"***"`.
   - `renault_mqtt/runtime.py:163-172` (`run_main`):
     - `if not cfg_fn(req):` treats `"null"` as present for `USERNAME`, `PASSWORD`, and `VIN`.
     - `int(cfg_fn(f"{prefix}POLL_INTERVAL", "300") or "300")`,
       `float(cfg_fn(f"{prefix}BATTERY_CAPACITY_KWH", "52") or "52")`, and
       `int(cfg_fn(f"{prefix}STALE_HOURS", "6") or "6")` raise `ValueError` on `"null"` because
       `"null"` is truthy and bypasses the `or` fallback.

Disproving check against `renault-mqtt` `a163aef`:

```sh
python3 -c '
import os, renault_mqtt.config as c, renault_mqtt.commands as cmd
c.ENV_PREFIX = "A290_"
os.environ["A290_PRECONDITION_TEMPERATURE"] = "null"
os.environ["A290_ACCOUNT_ID"] = "null"
assert c.cfg("A290_ACCOUNT_ID") == "null"
assert c.redact("payload has null field") == "payload has *** field"
cmd.precondition_temp()
'
```

Running that command prints `ValueError: could not convert string to float: 'null'`.

## 2. Options, costs, and failures each option still permits

| Option | What changes | Cost | Failure still permitted |
| --- | --- | --- | --- |
| **1. Do nothing in core (`run.sh` `opt()` only)** | Keep `renault-mqtt` unchanged. Each add-on's `run.sh` normalises `"null"` to `""` via `opt()` on optional settings (`account_id`, `precondition_temperature`), as shipped in `a290-ha-addon` 1.30.3 (#309). | **Zero** in `renault-mqtt`; no core release or `CORE_REF` bump. | (a) A new optional setting added in `a290`, `r5`, or a third model (`docs/adding-a-model.md`) exported with `bashio::config` instead of `opt` passes `"null"` into `renault-mqtt` (`ValueError`, broken auto-discovery, or `"null"` redaction).<br>(b) Any setting in `run.sh` that still uses plain `bashio::config` (`username`, `password`, `vin`, `poll_interval`, `battery_capacity_kwh`, `stale_hours`) passes `"null"` if absent or `null` in `/data/options.json`, bypassing `run_main`'s missing-config check or crashing `int()`/`float()` at startup.<br>(c) `_opt_flag` and `cfg` in `renault_mqtt/config.py` retain contradictory `"null"` semantics. |
| **2. Core hardening only (drop `opt()` in `run.sh`)** | Harden `renault-mqtt` to treat `"null"` as unset (in `cfg()` or in `precondition_temp`, `resolve_account`, `_config_secrets`, and `run_main`), and revert `opt()` in the add-ons' `run.sh`. | One `renault-mqtt` PR + release, unit tests in `test_config.py` / `test_commands.py` / `test_runtime.py`, plus `CORE_REF` and `run.sh` PRs in both add-ons. | (a) Add-on code that reads `os.environ` directly without calling `renault_mqtt.config.cfg` (e.g. `A290_GPS_PRECISION` in `main.py:91`, `A290_DEPLOY_DASHBOARD`, `A290_DASHBOARD_URL_PATH`, and `A290_REDEPLOY_DASHBOARD` in `deploy.py:492-508`) still receives `"null"` from `run.sh`; e.g. `A290_DASHBOARD_URL_PATH="null"` fails `_validate_url_path` instead of falling back to `"alpine-a290"`.<br>(b) If `cfg()` strips `"null"` unconditionally for every key, a literal password of `"null"` (or `"NULL"`) is coerced to `""` and rejected at startup as missing configuration. |
| **3. Both (`run.sh` `opt()` + core hardening)** | Keep `opt()` in `run.sh` (and document it in `docs/adding-a-model.md`) **and** harden `renault-mqtt` against `"null"` in `precondition_temp`, `resolve_account`, `_config_secrets`, and `run_main` (or in `cfg()` for non-password settings). | One `renault-mqtt` PR + release + unit tests, plus `docs/adding-a-model.md` update; add-ons pick it up on the next `CORE_REF` bump with no urgent release needed (`opt()` is already live). | (a) If `"null"` normalisation is applied globally in `cfg()`, a literal value of `"null"` cannot be passed through `cfg()` (e.g. a password literally equal to `"null"`).<br>(b) If `"null"` normalisation in core is scoped to specific readers (`precondition_temp`, `resolve_account`, `_config_secrets`, `run_main` numbers/required-keys) rather than `cfg()` itself, a future raw `cfg()` caller in core combined with a missing `opt()` in `run.sh` could still observe `"null"`.<br>(c) Add-on-local `os.environ.get` readers (`deploy.py`) still depend on `run.sh` using `opt()` (or their own check, as `_charger_eid` already has at `deploy.py:150`). |

## 3. Sub-choice if Option 2 or Option 3 is selected: where in `renault-mqtt` to normalise

If core hardening is chosen, there are two implementation shapes inside `renault-mqtt`:

- **Shape A — Normalise in `cfg()` (`renault_mqtt/config.py`):**
  ```python
  def cfg(name, default=""):
      v = os.environ.get(name)
      if v is None or v.strip().lower() in ("", "null"):
          return default
      return v
  ```
  - *Pros:* Single point of truth in `config.py`, matching `_opt_flag`; protects every current and
    future caller (`precondition_temp`, `resolve_account`, `_config_secrets`, `run_main`).
  - *Cons:* Cannot represent a literal `"null"` string for any variable read via `cfg()` (notably
    `<PREFIX>PASSWORD` if a user's password is literally `null`, though `bashio::config` itself also
    cannot return a literal `"null"` when `result == "null"` unless `default_value` happens to be
    `"null"`).

- **Shape B — Keep `cfg()` raw; add an optional/unset helper or normalise at the option readers:**
  Add `opt_cfg(name, default="")` (or normalise `"null"` in `precondition_temp`, `resolve_account`,
  `_config_secrets`, and `run_main`), leaving `cfg()` raw or using `opt_cfg()` everywhere except
  `PASSWORD`.
  - *Pros:* Never alters a literal password value; explicitly separates raw env lookup from
    `bashio`-exported option reading.
  - *Cons:* Callers must choose `opt_cfg` vs `cfg`.

## 4. Decision required from the owner

1. Choose **Option 1** (do nothing in core; `run.sh` `opt()` only), **Option 2** (core hardening
   only), or **Option 3** (both `run.sh` `opt()` and core hardening).
2. If Option 2 or 3 is chosen, choose **Shape A** (`cfg()` normalises `""` and `"null"` to
   `default`) or **Shape B** (dedicated helper / reader-level normalisation).
