"""The core's real discovery output, recorded and replayed.

Payloads are never hand-written: the core under test is imported from a checkout given by path,
``publish_discovery`` runs against a recording client, and the recording is replayed to the broker
in the order the core produced it, empty payloads included. The one edit made afterwards is
per-scenario isolation (device identifier, unique_id and topic node get a suffix) so scenarios
sharing one HA cannot interfere. Anything else that departs from the core's output is labelled
where it happens, and the report carries the label.
"""
import copy
import importlib
import json
import os
import sys
import threading
import time
import types

import paho.mqtt.client as paho_mqtt

DISCOVERY_PREFIX = "homeassistant"
CLASSES = ("sensor", "binary_sensor", "button", "number")


def load_core(core_path):
    """Import renault_mqtt from `core_path`, and prove that is where it came from."""
    core_path = os.path.realpath(core_path)
    sys.path.insert(0, core_path)
    pkg = importlib.import_module("renault_mqtt")
    got = os.path.realpath(os.path.dirname(pkg.__file__))
    if got != os.path.join(core_path, "renault_mqtt"):
        raise SystemExit(f"renault_mqtt imported from {got}, not from --core {core_path}")
    config = importlib.import_module("renault_mqtt.config")
    mqtt = importlib.import_module("renault_mqtt.mqtt")
    return types.SimpleNamespace(path=core_path, version=pkg.__version__, config=config, mqtt=mqtt)


def load_catalog(spec):
    """`spec` is SYS_PATH_ENTRY:MODULE, e.g. /path/to/alpine_a290/app:catalog."""
    entry, _, module = spec.rpartition(":")
    sys.path.insert(0, entry)
    try:
        sys.modules.pop(module, None)
        return importlib.import_module(module)
    finally:
        sys.path.remove(entry)


class Recorder:
    """Stands in for the paho client: records every publish, in order, empty payloads included."""

    def __init__(self):
        self.log = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.log.append({"topic": topic, "payload": "" if payload is None else payload,
                         "retain": retain, "qos": qos})


def all_endpoints(cat):
    return (set(cat.OPTIONAL_ENDPOINTS) | {ep for _, _, ep in cat.ACTION_BUTTONS.values()}
            | {cat.SOC_ENDPOINT})


def record(core, cat, supported=None, env=None):
    """Run the core's publish_discovery for `cat` and return its publishes in order."""
    for k, v in (env or {}).items():
        os.environ[k] = v
    core.config.ENV_PREFIX = cat.ENV_PREFIX
    core.mqtt.configure(cat)
    rec = Recorder()
    core.mqtt.publish_discovery(rec, all_endpoints(cat) if supported is None else supported, "km")
    return rec.log


# ---------------------------------------------------------------------- synthetic catalog
SYNTH_NODE = "rmqtt_e2e"
SYNTH_PREFIX = "e2e_"


def synth_catalog(device_name, sensors=None, binary=None, buttons=None, numbers=None, **retired):
    """The shape of tests/test_mqtt.py's fake catalog. Each scenario passes its own device name:
    if every scenario's device shared one, later scenarios would collide on entity ids and gain
    `_2` suffixes that say nothing about naming."""
    cat = types.SimpleNamespace(
        NODE=SYNTH_NODE, OBJ_PREFIX=SYNTH_PREFIX, ENV_PREFIX="E2E_", MQTT_KEEPALIVE=60,
        DEVICE={"identifiers": [SYNTH_NODE], "name": device_name, "manufacturer": "E2E",
                "model": "Synthetic"},
        DIST_UNIT_OBJS=("e2e_range",),
        SENSORS={"e2e_battery": ("Battery", "battery", "%", "measurement"),
                 "e2e_range": ("Range", "distance", "km", "measurement")} if sensors is None else sensors,
        BINARY_SENSORS={"e2e_plug": ("Plug", "plug")} if binary is None else binary,
        ICONS={},
        OPTIONAL_ENDPOINTS={},
        RETIRED_SENSORS=[],
        DEFAULT_DISABLED_SENSORS=set(),
        DATA_GATED_SENSORS=set(),
        ACTION_BUTTONS={"e2e_wake": ("Wake", "mdi:bell", "actions/wake")} if buttons is None else buttons,
        NUMBERS={"e2e_soc_min": ("SoC Min", "mdi:battery", 20, 80, 5)} if numbers is None else numbers,
        SOC_ENDPOINT="soc-levels",
        REFRESH_LOCATION_EP="actions/refresh-location",
    )
    for k, v in retired.items():
        setattr(cat, k, v)
    return cat


def synth_endpoints(cat):
    return all_endpoints(cat)


# ---------------------------------------------------------------------- record inspection
def config_topic_parts(topic):
    """homeassistant/<domain>/<node>/<obj>/config -> (domain, node, obj), else None."""
    parts = topic.split("/")
    if len(parts) == 5 and parts[0] == DISCOVERY_PREFIX and parts[4] == "config":
        return parts[1], parts[2], parts[3]
    return None


def configs(records):
    """{unique_id: (domain, record, parsed payload)} for every non-empty discovery config."""
    out = {}
    for r in records:
        tp = config_topic_parts(r["topic"])
        if tp and r["payload"]:
            conf = json.loads(r["payload"])
            out[conf["unique_id"]] = (tp[0], r, conf)
    return out


def topic_of(records, uid):
    """The discovery topic the core used for `uid`."""
    return configs(records)[uid][1]["topic"]


def tombstones(records):
    return [r for r in records if config_topic_parts(r["topic"]) and r["payload"] == ""]


def capabilities(core):
    """What the core under test can do, established from its OUTPUT rather than its source."""
    probe = synth_catalog("Probe Car", RETIRED_SENSORS=["e2e_probe_s"],
                          RETIRED_BINARY_SENSORS=["e2e_probe_bs"], RETIRED_BUTTONS=["e2e_probe_btn"],
                          RETIRED_NUMBERS=["e2e_probe_num"])
    recs = record(core, probe)
    dead = {config_topic_parts(r["topic"])[0] for r in tombstones(recs) if "probe" in r["topic"]}
    has_dei = any("default_entity_id" in conf for _, _, conf in configs(recs).values())
    return {"default_entity_id": has_dei, "retire": {c: c in dead for c in CLASSES}}


# ---------------------------------------------------------------------- per-scenario isolation
def isolate(records, sfx, node=SYNTH_NODE):
    """Suffix the topic node, unique_id and device identifier. default_entity_id is left exactly
    as the core computed it: scenarios differ by device name, so it is already distinct."""
    if sfx is None:
        return copy.deepcopy(records)
    new_node = f"{node}_{sfx}"

    def retopic(t):
        if isinstance(t, str) and (t == node or t.startswith(node + "/")):
            return new_node + t[len(node):]
        return t

    out = []
    for r in records:
        r = dict(r)
        tp = config_topic_parts(r["topic"])
        if tp:
            r["topic"] = f"{DISCOVERY_PREFIX}/{tp[0]}/{new_node}/{tp[2]}/config"
        else:
            r["topic"] = retopic(r["topic"])
        if tp and r["payload"]:
            conf = json.loads(r["payload"])
            conf["unique_id"] = f"{conf['unique_id']}_{sfx}"
            conf["device"] = dict(conf["device"], identifiers=[f"{i}_{sfx}" for i in conf["device"]["identifiers"]])
            for k, v in list(conf.items()):
                conf[k] = retopic(v)
            if "availability" in conf:
                conf["availability"] = [dict(a, topic=retopic(a["topic"])) for a in conf["availability"]]
            r["payload"] = json.dumps(conf)
        out.append(r)
    return out


def edit_config(records, uid, fn, label):
    """Apply `fn(conf)` to the config for `uid`. Every use is a departure from the core's output,
    so it must carry a label that ends up in the report."""
    out = []
    for r in records:
        tp = config_topic_parts(r["topic"])
        if tp and r["payload"] and json.loads(r["payload"])["unique_id"] == uid:
            conf = json.loads(r["payload"])
            fn(conf)
            r = dict(r, payload=json.dumps(conf), edited=label)
        out.append(r)
    return out


def strip_default_entity_id(records, uids=None):
    out = []
    for r in records:
        tp = config_topic_parts(r["topic"])
        if tp and r["payload"]:
            conf = json.loads(r["payload"])
            if "default_entity_id" in conf and (uids is None or conf["unique_id"] in uids):
                conf.pop("default_entity_id")
                r = dict(r, payload=json.dumps(conf), edited="default_entity_id removed")
        out.append(r)
    return out


def describe(records):
    """The publish order, compactly: one line per publish."""
    lines = []
    for r in records:
        tp = config_topic_parts(r["topic"])
        if tp:
            kind = "TOMBSTONE" if r["payload"] == "" else "CONFIG"
            uid = json.loads(r["payload"])["unique_id"] if r["payload"] else ""
            extra = f" [{r['edited']}]" if r.get("edited") else ""
            lines.append(f"{kind} {tp[0]}/{tp[2]} {uid}{extra}".rstrip())
        else:
            lines.append(f"STATE {r['topic']} {r['payload']!r}")
    return lines


# ---------------------------------------------------------------------- broker
class Broker:
    def __init__(self, port):
        self.port = port
        self.client = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION2, client_id="rmqtt-e2e-replay")
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()

    def replay(self, records):
        """Publish in recorded order. retain is the core's own flag (always True for these), qos
        the core's own (the default 0), so the wire sequence is what production would send."""
        for r in records:
            if not r["retain"]:
                raise AssertionError(f"core published {r['topic']} unretained; replay would diverge")
            info = self.client.publish(r["topic"], r["payload"], qos=r["qos"], retain=True)
            info.wait_for_publish(timeout=10)

    def retained(self, pattern, wait=3.0):
        """Snapshot what the broker would hand a NEW subscriber on `pattern` (retained only)."""
        got = {}
        lock = threading.Lock()
        c = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION2, client_id=f"rmqtt-e2e-snap-{time.time_ns()}")

        def on_message(_c, _u, msg):
            if msg.retain:
                with lock:
                    got[msg.topic] = msg.payload.decode()

        c.on_message = on_message
        c.connect("127.0.0.1", self.port)
        c.subscribe(pattern)
        c.loop_start()
        time.sleep(wait)
        c.loop_stop()
        c.disconnect()
        return got

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()
