# renault-mqtt

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
    "renault-mqtt @ git+https://github.com/MatthewHobbs/renault-mqtt@<immutable-sha>"
```

The ref is pinned by **immutable commit SHA** (a tag is kept alongside for readability), the same
reproducibility discipline the add-ons already use for the bundled `tempio` rebuild. Bump it
deliberately when a core change should ship; the add-on's own container-verify + CI gate confirms
the new core renders and boots clean.

## Modules

| Module   | Holds |
|----------|-------|
| `util`   | Pure primitives (`now_ts` / `iso` / `_num`). |
| `config` | `cfg` / `_opt_flag` and the secret-redaction net; env prefix injected via `ENV_PREFIX`. |
| `charge` | Charge-session reconciliation and `CHARGES_ENDPOINT`. |
| `debug`  | The one-shot debug dump and its payload redaction. |
| `mqtt`   | Discovery and the broker client; identity and tables injected via `configure(catalog)`. |
| `parse`  | Shared value-format and payload-parse helpers. |

Per-model variation stays out of here: `catalog.py`, `poll_once`, controls, device/node
identifiers, endpoint quirks and default capacity live in each add-on. To add a model, see
[docs/adding-a-model.md](docs/adding-a-model.md).

## Development

```sh
pip install -e . -r requirements-dev.txt
just ci        # ruff + pytest at 100 % coverage, the same commands CI runs
just e2e 2026.9.3   # optional: the Home Assistant harness in Docker, see tests/e2e/README.md
```

MIT licensed.
