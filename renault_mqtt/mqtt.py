"""MQTT integration seam shared by the Renault-platform add-ons: the broker client, its
connect/message/disconnect callbacks, and Home Assistant MQTT-discovery publishing. Owns the
topic topology (state/attributes/availability/command topics) and the location-publish policy.

Per-model identity and the discovery tables are injected by the add-on via ``configure(catalog)``
at startup — this module imports the config leaf only, never an add-on module. ``configure`` reads
from the passed catalog:
  * ``NODE`` — the HA discovery node / topic root (e.g. "alpine_a290" / "renault_5");
  * ``DEVICE`` — the HA device block;
  * ``OBJ_PREFIX`` — the object_id prefix stripped to form value_template keys / command suffixes;
  * ``DIST_UNIT_OBJS`` — the sensor object_ids whose unit follows the locale (mi/km);
  * ``MQTT_KEEPALIVE`` — the broker keepalive (optional, default 60);
  * the discovery tables ``SENSORS`` / ``BINARY_SENSORS`` / ``ICONS`` / ``ACTION_BUTTONS`` /
    ``NUMBERS`` / ``OPTIONAL_ENDPOINTS`` / ``RETIRED_SENSORS`` / ``DEFAULT_DISABLED_SENSORS`` and
    the endpoint names ``SOC_ENDPOINT`` / ``REFRESH_LOCATION_EP``.

The one edge that would otherwise point back at the add-on's poll loop (an inbound command must run
the add-on's async run_command on the event loop) is inverted via injection: the add-on sets
``mqtt._LOOP`` and ``mqtt._COMMAND_HANDLER`` at startup and ``_on_message`` calls the injected
handler — so the dependency stays one-directional with no cycle.
"""
import asyncio
import json
import logging

import paho.mqtt.client as paho_mqtt

from renault_mqtt import config
from renault_mqtt.config import _opt_flag, cfg

LOG = logging.getLogger("renault_mqtt.mqtt")

DISCOVERY_PREFIX = "homeassistant"

# Per-model identity + discovery tables, injected by configure() at startup. None until then.
_CAT = None
NODE = None
DEVICE = None
_KEEPALIVE = 60
_CLIENT_ID = None
_DIST_UNIT_OBJS = frozenset()
STATE_TOPIC = None
ATTR_TOPIC = None
TRACKER_STATE_TOPIC = None
AVAIL_TOPIC = None
CMD_PREFIX = None

# Location publishing is opt-out (default on). Gates the device_tracker discovery entity, the
# refresh-location button, and (in the poll loop) the GPS read + refresh command. Single source of
# truth — the add-on reads it as mqtt.PUBLISH_LOCATION. Set by configure() (needs the env prefix).
PUBLISH_LOCATION = None

# Injected by the add-on at startup (see module docstring). _LOOP is the running event loop an
# inbound command is scheduled onto; _COMMAND_HANDLER is the add-on's async run_command(cmd, payload).
_LOOP = None
_COMMAND_HANDLER = None

# Carries the current supported-endpoint set + distance unit so _on_connect can re-publish discovery
# on every reconnect (survives a broker restart). Set by the add-on before mqtt_connect().
_MQTT_CTX = {"supported": None, "dist_unit": None}


def configure(catalog):
    """Inject the add-on's catalog + derive the per-model MQTT identity. Run once at startup, AFTER
    config.ENV_PREFIX is injected (PUBLISH_LOCATION is read here under that prefix)."""
    global _CAT, NODE, DEVICE, _KEEPALIVE, _CLIENT_ID, _DIST_UNIT_OBJS
    global STATE_TOPIC, ATTR_TOPIC, TRACKER_STATE_TOPIC, AVAIL_TOPIC, CMD_PREFIX, PUBLISH_LOCATION
    _CAT = catalog
    NODE = catalog.NODE
    DEVICE = catalog.DEVICE
    _KEEPALIVE = getattr(catalog, "MQTT_KEEPALIVE", 60)
    _CLIENT_ID = f"{NODE}_addon"
    _DIST_UNIT_OBJS = frozenset(catalog.DIST_UNIT_OBJS)
    STATE_TOPIC = f"{NODE}/state"
    ATTR_TOPIC = f"{NODE}/location/attributes"
    TRACKER_STATE_TOPIC = f"{NODE}/location/state"
    AVAIL_TOPIC = f"{NODE}/availability"
    CMD_PREFIX = f"{NODE}/cmd/"
    PUBLISH_LOCATION = _opt_flag(config.ENV_PREFIX + "PUBLISH_LOCATION", True)


def _on_message(client, userdata, msg):
    if _LOOP is not None and _COMMAND_HANDLER is not None and msg.topic.startswith(CMD_PREFIX):
        cmd = msg.topic[len(CMD_PREFIX):]
        payload = msg.payload.decode(errors="replace") if msg.payload else ""
        LOG.info("Received command: %s %s", cmd, payload)
        asyncio.run_coroutine_threadsafe(_COMMAND_HANDLER(cmd, payload), _LOOP)


def _on_connect(client, userdata, flags, reason_code, properties=None):
    # Guard on a successful CONNACK: on a refused connect (rc != 0) the client isn't connected, so
    # don't claim "online" or attempt (no-op) subscribes/republishes — paho will retry via the
    # bounded backoff, and this runs again on the eventual success. Runs on the initial connect AND
    # every reconnect, so the discovery re-publish (from _MQTT_CTX) survives a broker restart.
    if reason_code != 0:
        LOG.warning("MQTT connect refused: %s", reason_code)
        return
    client.subscribe(f"{CMD_PREFIX}#")
    if _MQTT_CTX["supported"] is not None:
        publish_discovery(client, _MQTT_CTX["supported"], _MQTT_CTX["dist_unit"])
    client.publish(AVAIL_TOPIC, "online", retain=True)
    LOG.info("MQTT connected — subscribed to commands, discovery (re)published")


def _on_disconnect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        LOG.warning("MQTT disconnected (%s) — reconnecting", reason_code)


def mqtt_connect():
    client = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION2, client_id=_CLIENT_ID)
    if cfg("MQTT_USER"):
        client.username_pw_set(cfg("MQTT_USER"), cfg("MQTT_PASS"))
    client.will_set(AVAIL_TOPIC, "offline", retain=True)
    client.on_message = _on_message
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=120)   # bounded backoff on broker drop
    LOG.info("Connecting to MQTT %s:%s", cfg("MQTT_HOST"), cfg("MQTT_PORT", "1883"))
    client.connect(cfg("MQTT_HOST"), int(cfg("MQTT_PORT", "1883") or "1883"), keepalive=_KEEPALIVE)
    client.loop_start()
    return client


def publish_discovery(client, supported_eps, dist_unit):
    cat = _CAT
    prefix = cat.OBJ_PREFIX
    skip = {obj for ep, objs in cat.OPTIONAL_ENDPOINTS.items()
            if ep not in supported_eps for obj in objs}
    for obj in set(skip) | set(cat.RETIRED_SENSORS):
        client.publish(f"{DISCOVERY_PREFIX}/sensor/{NODE}/{obj}/config", "", retain=True)
    published = 0
    for obj, (name, dev_class, unit, state_class) in cat.SENSORS.items():
        if obj in skip:
            continue
        published += 1
        if obj in _DIST_UNIT_OBJS:
            unit = dist_unit
            if dist_unit == "mi":
                dev_class = None  # else HA (metric) re-converts our miles back to km
        conf = {"name": name, "object_id": obj, "unique_id": obj,
                "state_topic": STATE_TOPIC, "value_template": "{{ value_json.%s }}" % obj.removeprefix(prefix),
                "availability_topic": AVAIL_TOPIC, "device": DEVICE}
        if dev_class:
            conf["device_class"] = dev_class
        if unit:
            conf["unit_of_measurement"] = unit
        if state_class:
            conf["state_class"] = state_class
        if obj in cat.ICONS:
            conf["icon"] = cat.ICONS[obj]
        if obj in cat.DEFAULT_DISABLED_SENSORS:
            conf["enabled_by_default"] = False
        client.publish(f"{DISCOVERY_PREFIX}/sensor/{NODE}/{obj}/config", json.dumps(conf), retain=True)
    for obj, (name, dev_class) in cat.BINARY_SENSORS.items():
        conf = {"name": name, "object_id": obj, "unique_id": obj,
                "state_topic": STATE_TOPIC, "value_template": "{{ value_json.%s }}" % obj.removeprefix(prefix),
                "payload_on": "on", "payload_off": "off",
                "availability_topic": AVAIL_TOPIC, "device": DEVICE}
        if dev_class:
            conf["device_class"] = dev_class
        if obj in cat.ICONS:
            conf["icon"] = cat.ICONS[obj]
        client.publish(f"{DISCOVERY_PREFIX}/binary_sensor/{NODE}/{obj}/config", json.dumps(conf), retain=True)
    tracker_topic = f"{DISCOVERY_PREFIX}/device_tracker/{NODE}/location/config"
    if PUBLISH_LOCATION:
        loc_id = f"{prefix}car_location"
        tracker = {"name": "Location", "object_id": loc_id, "unique_id": loc_id,
                   "state_topic": TRACKER_STATE_TOPIC, "json_attributes_topic": ATTR_TOPIC,
                   "availability_topic": AVAIL_TOPIC, "source_type": "gps", "device": DEVICE}
        client.publish(tracker_topic, json.dumps(tracker), retain=True)
    else:
        # Location opt-out: remove the tracker entity and clear any GPS previously retained on the
        # broker so an earlier fix doesn't linger after the user turns location off.
        client.publish(tracker_topic, "", retain=True)
        client.publish(ATTR_TOPIC, "", retain=True)
        client.publish(TRACKER_STATE_TOPIC, "", retain=True)
    buttons = []
    # The discovery node segment + object_id derive from the object_id (short); the command suffix
    # is the same by default, but a model whose command name differs from its entity id can remap it
    # via the optional catalog.BUTTON_CMD_OVERRIDES {object_id: cmd_suffix} — e.g. the R5 ships
    # object_id "r5_flash_lights" but commands on "lights". a290 has no overrides (cmd == short).
    cmd_overrides = getattr(cat, "BUTTON_CMD_OVERRIDES", {})
    for obj, (name, icon, ep) in cat.ACTION_BUTTONS.items():
        short = obj.removeprefix(prefix)
        cmd = cmd_overrides.get(obj, short)
        topic = f"{DISCOVERY_PREFIX}/button/{NODE}/{short}/config"
        # Suppress the location-refresh button too when the user has opted out of location.
        if ep in supported_eps and not (ep == cat.REFRESH_LOCATION_EP and not PUBLISH_LOCATION):
            conf = {"name": name, "object_id": obj, "unique_id": obj,
                    "command_topic": f"{CMD_PREFIX}{cmd}", "availability_topic": AVAIL_TOPIC,
                    "icon": icon, "device": DEVICE}
            client.publish(topic, json.dumps(conf), retain=True)
            buttons.append(short)
        else:
            client.publish(topic, "", retain=True)
    numbers = []
    soc_ok = cat.SOC_ENDPOINT in supported_eps
    for obj, (name, icon, mn, mx, step) in cat.NUMBERS.items():
        short = obj.removeprefix(prefix)
        topic = f"{DISCOVERY_PREFIX}/number/{NODE}/{short}/config"
        if soc_ok:
            conf = {"name": name, "object_id": obj, "unique_id": obj,
                    "state_topic": STATE_TOPIC, "value_template": "{{ value_json.%s }}" % short,
                    "command_topic": f"{CMD_PREFIX}{short}", "availability_topic": AVAIL_TOPIC,
                    "min": mn, "max": mx, "step": step, "mode": "slider",
                    "unit_of_measurement": "%", "device_class": "battery",
                    "optimistic": True, "icon": icon, "device": DEVICE}
            client.publish(topic, json.dumps(conf), retain=True)
            numbers.append(short)
        else:
            client.publish(topic, "", retain=True)
    LOG.info("Published discovery: %d sensors (%d unsupported cleared), %d binary_sensors, "
             "location=%s, buttons=%s, numbers=%s",
             published, len(skip), len(cat.BINARY_SENSORS),
             "on" if PUBLISH_LOCATION else "off (cleared)", buttons or "none", numbers or "none")
