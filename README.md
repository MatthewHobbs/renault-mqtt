# renault-ha-core

Shared core for the Renault-platform Home Assistant add-ons —
[**a290-ha-addon**](https://github.com/MatthewHobbs/a290-ha-addon) (Alpine A290) and
[**r5-ha-addon**](https://github.com/MatthewHobbs/r5-ha-addon) (Renault 5). Both poll the
Renault/Kamereon API (`renault-api`) and publish MQTT auto-discovery entities. Roughly 90 % of
that code is identical between the two; this package holds it **once**, parameterised by each
add-on's per-model `catalog.py`, instead of being hand-mirrored between the sibling repos.

A missed manual mirror is a silently dead sensor with no CI failure in the sibling repo. This
package exists to make that class of drift impossible before a third Renault-platform model
lands. See [a290-ha-addon#56](https://github.com/MatthewHobbs/a290-ha-addon/issues/56).

## How the add-ons consume it

Each add-on's Docker image installs this package from a pinned, immutable git ref, in a separate
`pip install --no-deps` step so the add-on's hash-locked `requirements.txt` stays the single
source of truth for the third-party dependency tree:

```dockerfile
# after the hash-locked requirements.txt install
RUN pip3 install --no-cache-dir --break-system-packages --no-deps \
    "renault-ha-core @ git+https://github.com/MatthewHobbs/renault-ha-core@<immutable-sha>"
```

The ref is pinned by **immutable commit SHA** (a tag is kept alongside for readability), the same
reproducibility discipline the add-ons already use for the bundled `tempio` rebuild. Bump it
deliberately when a core change should ship; the add-on's own container-verify + CI gate confirms
the new core renders and boots clean.

## Extraction status

The modules move over incrementally — one small PR per seam, tests green throughout — mirroring
the `main.py` split that preceded this (a290 #55). Order runs safest-leaf-first:

| Module    | Status  | Notes                                                             |
|-----------|---------|-------------------------------------------------------------------|
| `util`    | **here**| Pure primitives (`now_ts` / `iso` / `_num`). The walking skeleton.|
| `config`  | **here**| `cfg` / `_opt_flag` / redaction net; env prefix injected via `ENV_PREFIX`.|
| `charge`  | pending | Charge-session reconciliation.                                    |
| `debug`   | pending | Debug-dump + redaction (r5 has an extra `alerts` probe).          |
| `mqtt`    | pending | Discovery + client (a290 has `_MQTT_CTX`; keepalive/DEVICE differ).|
| `main`    | pending | Poll loop + controls, parameterised by catalog.                   |

Per-model variation stays out of here: `catalog.py`, device/node identifiers, endpoint quirks and
default capacity live in each add-on.

## Development

```sh
pip install -e .
ruff check renault_ha_core
python3 -m pytest tests -q --cov=renault_ha_core --cov-report=term-missing --cov-fail-under=100
```

MIT licensed.
