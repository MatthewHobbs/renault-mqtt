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
        self.subs = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.log.append({"topic": topic, "payload": "" if payload is None else payload,
                         "retain": retain, "qos": qos})

    def subscribe(self, topic, *args, **kwargs):
        topics = topic if isinstance(topic, list) else [topic]
        self.subs += [t if isinstance(t, str) else t[0] for t in topics]


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
    """{unique_id: (domain, record, parsed payload)} for every retained, non-empty discovery config.
    An unretained config is a resurrect ahead of a tombstone, not an entity meant to exist."""
    out = {}
    for r in records:
        tp = config_topic_parts(r["topic"])
        if tp and r["payload"] and r["retain"]:
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
    resurrect = len(resurrected(recs)) == len(tombstones(recs)) > 0
    return {"default_entity_id": has_dei, "retire": {c: c in dead for c in CLASSES},
            "resurrect": resurrect, "birth": birth_republishes(core, probe)}


def resurrected(records):
    """Tombstones immediately preceded by an unretained config on the same topic."""
    return [b for a, b in zip(records, records[1:], strict=False)
            if config_topic_parts(b["topic"]) and b["payload"] == "" and b["retain"]
            and a["topic"] == b["topic"] and a["payload"] and not a["retain"]]


BIRTH_TOPIC = f"{DISCOVERY_PREFIX}/status"


def birth_republishes(core, cat):
    """Does the core subscribe to HA's birth topic on connect, and re-publish discovery when
    `online` arrives there? Driven through its real callbacks, not read from its source."""
    m = core.mqtt
    saved = dict(m._MQTT_CTX)
    try:
        m._MQTT_CTX.update(supported=all_endpoints(cat), dist_unit="km")
        rec = Recorder()
        m._on_connect(rec, None, None, 0)
        before = len(rec.log)
        m._on_message(rec, None, types.SimpleNamespace(topic=BIRTH_TOPIC, payload=b"online"))
        return BIRTH_TOPIC in rec.subs and any(config_topic_parts(r["topic"]) for r in rec.log[before:])
    finally:
        m._MQTT_CTX.clear()
        m._MQTT_CTX.update(saved)


# The one key each domain cannot be configured without.
REQUIRED_TOPIC = {"sensor": "state_topic", "binary_sensor": "state_topic", "button": "command_topic",
                  "number": "command_topic"}


def cycle(rec, conf, label, disabled=False):
    """[minimal config UNRETAINED, empty RETAINED] on `rec`'s topic: option C's sequence, built by
    the harness, so both records carry `label`. `disabled` adds enabled_by_default: false."""
    domain = config_topic_parts(rec["topic"])[0]
    minimal = {"unique_id": conf["unique_id"], "device": conf["device"],
               REQUIRED_TOPIC[domain]: conf[REQUIRED_TOPIC[domain]]}
    if disabled:
        minimal["enabled_by_default"] = False
    return [{"topic": rec["topic"], "payload": json.dumps(minimal), "retain": False, "qos": 0, "edited": label},
            {"topic": rec["topic"], "payload": "", "retain": True, "qos": 0, "edited": label}]


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
        """Publish in recorded order with the core's own retain flag and qos, so the wire sequence
        is what production would send."""
        for r in records:
            info = self.client.publish(r["topic"], r["payload"], qos=r["qos"], retain=r["retain"])
            info.wait_for_publish(timeout=10)

    def retained_when_up(self, pattern, timeout=30):
        """retained(), once a restarted broker accepts connections again."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.retained(pattern)
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)

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


class Watcher:
    """Records every message on `topics`, with the PUBLISHER's retain flag.

    A plain subscriber cannot answer "was it published retained?": the broker clears the flag on
    live delivery, so every reading would be False. MQTT 5 retain-as-published keeps it, and
    probe() shows the instrument reporting both values before any reading counts."""

    PROBE = "rmqtt-e2e/probe"

    def __init__(self, port, topics):
        self.msgs = []
        self._lock = threading.Lock()
        self._subscribed = threading.Event()
        opts = paho_mqtt.SubscribeOptions(qos=0, retainAsPublished=True)
        self._topics = [(t, opts) for t in [*topics, self.PROBE]]
        self.client = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION2,
                                       client_id=f"rmqtt-e2e-watch-{time.time_ns()}",
                                       protocol=paho_mqtt.MQTTv5)
        self.client.on_connect = lambda c, *_: c.subscribe(self._topics)
        self.client.on_subscribe = lambda *_: self._subscribed.set()
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()
        if not self._subscribed.wait(10):
            raise RuntimeError("watcher never subscribed")

    def _on_message(self, _c, _u, msg):
        with self._lock:
            self.msgs.append({"t": time.monotonic(), "wall": time.time(), "topic": msg.topic,
                              "payload": msg.payload.decode(errors="replace"), "retain": bool(msg.retain)})

    def probe(self):
        """Publish one retained and one unretained message; return the flags the watcher saw."""
        for payload, retain in (("probe-retained", True), ("probe-plain", False)):
            self.client.publish(self.PROBE, payload, retain=retain).wait_for_publish(timeout=10)
        self.client.publish(self.PROBE, "", retain=True).wait_for_publish(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            seen = {m["payload"]: m["retain"] for m in self.on(self.PROBE)}
            if "probe-retained" in seen and "probe-plain" in seen:
                return seen
            time.sleep(0.1)
        return {m["payload"]: m["retain"] for m in self.on(self.PROBE)}

    def on(self, topic, after=None):
        with self._lock:
            return [dict(m) for m in self.msgs if m["topic"] == topic and (after is None or m["t"] >= after)]

    def wait_for(self, topic, payload, after, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for m in self.on(topic, after):
                if m["payload"] == payload:
                    return m
            time.sleep(0.2)
        return None

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


class LiveCore:
    """The core's own MQTT client, started as both add-ons' main.py starts it: _MQTT_CTX set,
    mqtt_connect(), then an explicit publish_discovery. Its callbacks read module state, so
    nothing may call record() or configure() while one is running."""

    def __init__(self, core, cat, port, supported, env=None):
        for k, v in {"MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(port), "MQTT_USER": "", **(env or {})}.items():
            os.environ[k] = v
        core.config.ENV_PREFIX = cat.ENV_PREFIX
        core.mqtt.configure(cat)
        core.mqtt._MQTT_CTX.update(supported=set(supported), dist_unit="km")
        self.mqtt = core.mqtt
        self.client = core.mqtt.mqtt_connect()
        core.mqtt.publish_discovery(self.client, set(supported), "km")

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()
        self.mqtt._MQTT_CTX.update(supported=None, dist_unit=None)
