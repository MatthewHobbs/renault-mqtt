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
# after the hash-locked requirements.txt install; setuptools from apk, so nothing unpinned from PyPI
RUN apk add --no-cache --virtual .core-build git py3-setuptools \
 && pip3 install --no-cache-dir --break-system-packages --no-build-isolation --no-deps \
      "renault-mqtt @ git+https://github.com/MatthewHobbs/renault-mqtt@<immutable-sha>" \
 && apk del .core-build
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

### Retained poll snapshots

Inside the add-on's running asyncio loop, opt in to latest-snapshot delivery:

```python
client = mqtt.mqtt_connect(snapshots=True)
# After a poll (pass dictionaries, not pre-serialized JSON):
client.snapshots.update(data, location=location_attrs)
# Poll failure or no new location fix: preserve the last location.
client.snapshots.update(data)
# Explicit privacy clear: remove retained location attributes.
client.snapshots.update(data, location=None)
# At shutdown, before closing the asyncio loop:
await client.snapshots.close()
```

Use this in place of direct state, location and online publishes. Only pass
`location=location_attrs` when a new fix is available; an omitted location keeps the
last fix, whereas `None` clears it. `PUBLISH_LOCATION=false` never caches coordinates
and always clears retained attributes. Configuration remains once per process.

Updates must run on the creating event loop. They serialize a complete snapshot
immediately and return without waiting for a broker. The per-client cache holds only
the latest state and location, and survives delayed CONNACK and reconnect without
another poll. Network callbacks schedule replay after the existing ordered discovery
pass (including HA birth); snapshot writes run on the asyncio loop, so queued replay
cannot overwrite a newer poll with captured older data. Publish errors retry after one
second, using the latest snapshot. Messages remain retained QoS 0; this provides
reconnect recovery, not broker acknowledgements or persistence across process restarts.
An already in-flight snapshot may precede or interleave HA-birth discovery: clearing
readiness cannot retract QoS 0 writes already started. A complete latest-snapshot
replay follows discovery; the multi-topic snapshot is not an atomic transaction.
Discovery and command messages are not buffered.

Shutdown disables replay, gives offline availability up to two seconds to flush,
then disconnects, shuts down the current socket through Paho's public `socket()`
accessor, and joins the MQTT thread. This aborts pending writes if a broker stops
reading; offline delivery is best effort (QoS 0 completion is not broker receipt).
`close()` waits up to three seconds for a dedicated daemon cleanup thread, outside
the asyncio executor. If cleanup exceeds that budget, it logs a warning and returns;
cleanup and Paho's daemon may remain until the blocking operation returns or the
process exits. In particular, Paho has no public cancellation API for DNS resolution
or connection establishment (its default connect timeout is five seconds per socket
attempt, excluding DNS). This is a bounded wait, not a guarantee of thread termination
within three seconds. Existing `mqtt_connect()` callers retain raw-publish behavior
until ported.

## Development

```sh
pip install -e . -r requirements-dev.txt
just ci        # ruff + pytest at 100 % coverage, the same commands CI runs
just e2e 2026.9.3   # optional: the Home Assistant harness in Docker, see tests/e2e/README.md
```

Releases and tag signing: [docs/releasing.md](docs/releasing.md).

MIT licensed.
