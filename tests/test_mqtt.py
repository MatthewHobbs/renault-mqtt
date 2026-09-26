"""Tests for the mqtt seam: configure() identity injection, HA MQTT-discovery publishing, the
broker client wiring, and the connect/message/disconnect callbacks.

Model-agnostic: a small fake catalog is injected via configure() (as each add-on injects its real
one), and config.ENV_PREFIX is armed. The discovery-template/data-key contract — the class of bug
that ships broken dashboard tiles — is pinned here against that fake catalog; each add-on also
keeps a contract test against its real catalog.
"""
import json
import types

import pytest

from renault_mqtt import config, mqtt

_FAKE_CATALOG = types.SimpleNamespace(
    NODE="test_node",
    DEVICE={"identifiers": ["test_node"], "name": "Test Car", "manufacturer": "T", "model": "X"},
    OBJ_PREFIX="tst_",
    MQTT_KEEPALIVE=45,
    DIST_UNIT_OBJS=("tst_range",),
    SENSORS={
        "tst_battery": ("Battery", "battery", "%", "measurement"),
        "tst_range": ("Range", "distance", "km", "measurement"),   # unit follows locale
        "tst_pressure": ("Pressure", "pressure", "bar", None),      # optional -> skipped unsupported
        "tst_plain": ("Plain", None, None, None),                   # no dev_class/unit/state_class
        "tst_disabled": ("Disabled", None, None, None),             # default-disabled + icon
        "tst_gated": ("Gated", None, None, None),                   # availability follows its key
    },
    BINARY_SENSORS={"tst_plug": ("Plug", "plug"), "tst_flap": ("Flap", None)},
    ICONS={"tst_disabled": "mdi:foo", "tst_plug": "mdi:plug"},
    OPTIONAL_ENDPOINTS={"pressure": ["tst_pressure"]},
    RETIRED_SENSORS=["tst_old"],
    DEFAULT_DISABLED_SENSORS={"tst_disabled"},
    DATA_GATED_SENSORS={"tst_gated"},
    ACTION_BUTTONS={
        "tst_wake": ("Wake", "mdi:bell", "wake"),                        # supported
        "tst_refresh": ("Refresh", "mdi:map", "actions/refresh-location"),  # gated on location
        "tst_forbidden": ("Nope", "mdi:cancel", "forbidden-ep"),        # unsupported -> cleared
    },
    NUMBERS={"tst_soc_min": ("SoC Min", "mdi:battery", 20, 80, 5)},
    SOC_ENDPOINT="soc-levels",
    REFRESH_LOCATION_EP="actions/refresh-location",
)
_ALL_EPS = {"pressure", "wake", "actions/refresh-location", "soc-levels"}


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(config, "ENV_PREFIX", "TEST_")
    mqtt.configure(_FAKE_CATALOG)          # sets NODE/topics/DEVICE/PUBLISH_LOCATION from the fake
    monkeypatch.setattr(mqtt, "_LOOP", None)
    monkeypatch.setattr(mqtt, "_COMMAND_HANDLER", None)
    saved_ctx = dict(mqtt._MQTT_CTX)
    yield
    mqtt._MQTT_CTX.clear()
    mqtt._MQTT_CTX.update(saved_ctx)


class StubClient:
    """Captures MQTT publishes/subscribes so we can assert on discovery payloads."""

    def __init__(self):
        self.pub = {}
        self.subs = []
        # Ordered (topic, payload, retain) log. `pub` is last-write-wins per topic, which cannot
        # express a sequence of writes to the SAME topic -- exactly what the tracker state-topic
        # migration is. Assertions about ordering or retention must use this.
        self.log = []

    def publish(self, topic, payload, retain=False):
        self.pub[topic] = payload
        self.log.append((topic, payload, retain))

    def writes(self, topic):
        """Every (payload, retain) written to `topic`, in order."""
        return [(p, r) for t, p, r in self.log if t == topic]

    def index_of(self, topic, payload):
        """Position of a specific write in the overall publish order."""
        return next(i for i, (t, p, _) in enumerate(self.log) if t == topic and p == payload)

    def subscribe(self, topic):
        self.subs.append(topic)


# --------------------------------------------------------------------------- #
# configure()
# --------------------------------------------------------------------------- #
def test_configure_derives_identity_and_topics():
    assert mqtt.NODE == "test_node"
    assert mqtt.STATE_TOPIC == "test_node/state"
    assert mqtt.ATTR_TOPIC == "test_node/location/attributes"
    assert mqtt.TRACKER_STATE_TOPIC == "test_node/location/state"
    assert mqtt.AVAIL_TOPIC == "test_node/availability"
    assert mqtt.CMD_PREFIX == "test_node/cmd/"
    assert mqtt._CLIENT_ID == "test_node_addon"
    assert mqtt.DEVICE["name"] == "Test Car"
    assert mqtt.PUBLISH_LOCATION is True          # default when TEST_PUBLISH_LOCATION unset


# --------------------------------------------------------------------------- #
# discovery template / data-key contract
# --------------------------------------------------------------------------- #
def test_sensor_value_templates_strip_the_prefix_and_carry_fields():
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    conf = json.loads(c.pub["homeassistant/sensor/test_node/tst_battery/config"])
    assert conf["value_template"] == "{{ value_json.battery }}"       # prefix stripped
    assert conf["state_topic"] == "test_node/state" and conf["device"]["name"] == "Test Car"
    assert conf["device_class"] == "battery" and conf["unit_of_measurement"] == "%"
    # a plain sensor omits the optional keys
    plain = json.loads(c.pub["homeassistant/sensor/test_node/tst_plain/config"])
    assert "device_class" not in plain and "unit_of_measurement" not in plain and "state_class" not in plain
    # default-disabled + icon carried
    dis = json.loads(c.pub["homeassistant/sensor/test_node/tst_disabled/config"])
    assert dis["enabled_by_default"] is False and dis["icon"] == "mdi:foo"


def test_binary_sensor_templates_and_icon():
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    plug = json.loads(c.pub["homeassistant/binary_sensor/test_node/tst_plug/config"])
    assert plug["value_template"] == "{{ value_json.plug }}"
    assert plug["payload_on"] == "on" and plug["device_class"] == "plug" and plug["icon"] == "mdi:plug"
    flap = json.loads(c.pub["homeassistant/binary_sensor/test_node/tst_flap/config"])
    assert "device_class" not in flap and "icon" not in flap


def test_distance_unit_follows_locale_and_drops_device_class_for_miles():
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    km = json.loads(c.pub["homeassistant/sensor/test_node/tst_range/config"])
    assert km["unit_of_measurement"] == "km" and km["device_class"] == "distance"
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "mi")
    mi = json.loads(c.pub["homeassistant/sensor/test_node/tst_range/config"])
    assert mi["unit_of_measurement"] == "mi" and "device_class" not in mi   # HA would re-convert


def test_optional_sensor_cleared_when_endpoint_unsupported():
    c = StubClient()
    mqtt.publish_discovery(c, set(), "km")           # nothing supported -> pressure skipped+cleared
    assert c.pub["homeassistant/sensor/test_node/tst_pressure/config"] == ""
    assert "homeassistant/sensor/test_node/tst_battery/config" in c.pub   # non-optional still published


def test_retired_sensors_are_cleared():
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    assert c.pub["homeassistant/sensor/test_node/tst_old/config"] == ""


def test_location_tracker_published_and_cleared(monkeypatch):
    tracker = "homeassistant/device_tracker/test_node/location/config"
    c = StubClient()
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", True)
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    conf = json.loads(c.pub[tracker])
    assert conf["object_id"] == "tst_car_location" and conf["source_type"] == "gps"
    assert conf["json_attributes_topic"] == "test_node/location/attributes"
    # opt-out clears the tracker + retained GPS
    c = StubClient()
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", False)
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    assert c.pub[tracker] == "" and c.pub["test_node/location/attributes"] == ""
    assert c.pub["test_node/location/state"] == ""


def test_location_opt_out_gets_a_minimal_disabled_config_before_the_tombstone(monkeypatch):
    """The tracker is a retirement too (RFC 0009/C2): opting location out while HA is offline
    must not leave a permanent orphan any more than a RETIRED_SENSORS id would."""
    tracker = "homeassistant/device_tracker/test_node/location/config"
    c = StubClient()
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", False)
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    _assert_minimal_then_tombstone(c.writes(tracker), "tst_car_location",
                                    "json_attributes_topic", mqtt.ATTR_TOPIC)


def test_location_tracker_declares_no_state_topic(monkeypatch):
    """A state-topic payload becomes location_name, which wins over the lat/lon attributes and stops
    the entity ever resolving a zone. Its ABSENCE from the discovery config is the invariant."""
    c = StubClient()
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", True)
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    conf = json.loads(c.pub["homeassistant/device_tracker/test_node/location/config"])
    assert "state_topic" not in conf
    assert conf["json_attributes_topic"] == "test_node/location/attributes"


@pytest.mark.parametrize("publish_location", [True, False])
def test_location_state_topic_migration_is_ordered_and_retained(monkeypatch, publish_location):
    """The upgrade purge is a SEQUENCE, and each step is load-bearing:

      reset ("None", retained)  ->  discovery without state_topic  ->  tombstone ("", retained)

    Reset must land while a pre-upgrade HA still subscribes, or location_name is never cleared and
    the entity keeps reading "online" until a restart. The tombstone must land after the config, or
    the broker keeps a value for a future subscriber. Asserting only the final value would pass for
    a wrong order, a missing reset, or a non-retained write -- so assert the log, not `pub`.
    """
    state, config_topic = "test_node/location/state", "homeassistant/device_tracker/test_node/location/config"
    c = StubClient()
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", publish_location)
    mqtt.publish_discovery(c, _ALL_EPS, "km")

    assert c.writes(state) == [(mqtt.TRACKER_PAYLOAD_RESET, True), ("", True)]
    assert (c.index_of(state, mqtt.TRACKER_PAYLOAD_RESET)
            < c.index_of(config_topic, c.pub[config_topic])
            < c.index_of(state, ""))


def test_tracker_payload_reset_matches_ha_default():
    """HA's device_tracker payload_reset defaults to the literal string "None"; an empty payload is
    ignored and will NOT clear location_name. Pinned so the two never get conflated."""
    assert mqtt.TRACKER_PAYLOAD_RESET == "None"


def test_buttons_gated_on_support_and_location(monkeypatch):
    base = "homeassistant/button/test_node"
    c = StubClient()
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", True)
    monkeypatch.setattr(mqtt, "ENABLE_REFRESH_LOCATION", True)
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    wake = json.loads(c.pub[f"{base}/wake/config"])
    assert wake["command_topic"] == "test_node/cmd/wake" and wake["object_id"] == "tst_wake"
    assert json.loads(c.pub[f"{base}/refresh/config"])["name"] == "Refresh"   # both on -> shown
    assert c.pub[f"{base}/forbidden/config"] == ""                            # unsupported -> cleared
    # location off suppresses the refresh-location button too
    c = StubClient()
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", False)
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    assert c.pub[f"{base}/refresh/config"] == ""


@pytest.mark.parametrize("publish_location,enable_refresh,shown", [
    (True,  True,  True),    # opted in, location on -> the only combination that publishes it
    (True,  False, False),   # the shipped default
    (False, True,  False),   # opting in cannot override the location opt-out
    (False, False, False),
])
def test_refresh_location_button_is_opt_in(monkeypatch, publish_location, enable_refresh, shown):
    """The refresh-location button needs BOTH flags. It is destructive on a parked car: the press
    replaces the car's committed position with a 'no fix' answer on a CURRENT timestamp, and only a
    completed journey restores it (hacf-fr/renault-api#2250 §3). Withholding it must also CLEAR it,
    or an install that already has the entity keeps a working button after the upgrade."""
    topic = "homeassistant/button/test_node/refresh/config"
    c = StubClient()
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", publish_location)
    monkeypatch.setattr(mqtt, "ENABLE_REFRESH_LOCATION", enable_refresh)
    mqtt.publish_discovery(c, _ALL_EPS, "km")

    if shown:
        assert json.loads(c.pub[topic])["command_topic"] == "test_node/cmd/refresh"
    else:
        # A zero-length RETAINED payload is what makes HA delete the entity (preceded, since RFC
        # 0009/C2, by an unretained minimal disabled config -- see _assert_minimal_then_tombstone).
        # An absent publish would leave the pre-upgrade button in place, so assert the writes
        # themselves, not just the final value.
        assert c.writes(topic)[-1] == ("", True)


def test_refresh_location_defaults_to_off_when_unset():
    """_opt_flag falls back to its default when the env var is unset/''/'null' — the state of an
    upgraded install whose options have not been re-rendered. That must resolve to OFF, so a
    destructive action is never inherited silently by an upgrade."""
    assert mqtt.ENABLE_REFRESH_LOCATION is False   # default when TEST_ENABLE_REFRESH_LOCATION unset


def test_button_cmd_override_remaps_command_topic_only(monkeypatch):
    # A model whose command name differs from its entity id (e.g. R5: object_id "r5_flash_lights"
    # commanded on "lights") remaps ONLY the command suffix via BUTTON_CMD_OVERRIDES; the discovery
    # topic node + object_id stay derived from the object_id, so the entity id is unchanged.
    monkeypatch.setattr(_FAKE_CATALOG, "BUTTON_CMD_OVERRIDES", {"tst_wake": "wakeup"}, raising=False)
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    conf = json.loads(c.pub["homeassistant/button/test_node/wake/config"])   # node still 'wake'
    assert conf["object_id"] == "tst_wake"                                   # entity id unchanged
    assert conf["command_topic"] == "test_node/cmd/wakeup"                   # only the command remapped


def test_numbers_published_when_soc_supported_else_cleared():
    base = "homeassistant/number/test_node"
    c = StubClient()
    mqtt.publish_discovery(c, {"soc-levels"}, "km")
    conf = json.loads(c.pub[f"{base}/soc_min/config"])
    assert conf["min"] == 20 and conf["max"] == 80 and conf["step"] == 5 and conf["mode"] == "slider"
    assert conf["command_topic"] == "test_node/cmd/soc_min" and conf["value_template"] == "{{ value_json.soc_min }}"
    c = StubClient()
    mqtt.publish_discovery(c, set(), "km")           # soc-levels not supported -> cleared
    assert c.pub[f"{base}/soc_min/config"] == ""


# --------------------------------------------------------------------------- #
# default_entity_id — the entity id contract
# --------------------------------------------------------------------------- #
# Written out by hand rather than computed, so a test cannot agree with a wrong slug by construction.
_EXPECTED_IDS = {
    "sensor": {"Battery": "test_car_battery", "Range": "test_car_range", "Pressure": "test_car_pressure",
               "Plain": "test_car_plain", "Disabled": "test_car_disabled", "Gated": "test_car_gated"},
    "binary_sensor": {"Plug": "test_car_plug", "Flap": "test_car_flap"},
    "button": {"Wake": "test_car_wake", "Refresh": "test_car_refresh"},
    "number": {"SoC Min": "test_car_soc_min"},
    "device_tracker": {"Location": "test_car_location"},
}


def _published_configs(c):
    """Every non-empty discovery config, as (domain, conf)."""
    return [(t.split("/")[1], json.loads(p)) for t, p in c.pub.items()
            if t.startswith("homeassistant/") and p]


def test_every_published_config_pins_the_name_derived_entity_id(monkeypatch):
    """Without default_entity_id HA builds a NEW entity's id from the device's area and any user
    rename, so a car device in area "Auto" gets number.auto_r5_soc_max_target (r5 issue #83) and
    every dashboard reference misses. Every domain must pin the id, including the tracker."""
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", True)
    monkeypatch.setattr(mqtt, "ENABLE_REFRESH_LOCATION", True)
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")

    seen = {}
    for domain, conf in _published_configs(c):
        assert conf["default_entity_id"] == f"{domain}.{_EXPECTED_IDS[domain][conf['name']]}"
        assert "object_id" in conf                   # still sent: HA ignores it but accepts it
        seen.setdefault(domain, set()).add(conf["name"])
    # Guards the loop above: a domain that published nothing would otherwise pass vacuously.
    assert seen == {d: set(names) for d, names in _EXPECTED_IDS.items()}


def test_default_entity_id_follows_the_name_not_the_object_id():
    """object_id and the name-derived id differ for ten r5 entities; dashboards use the latter."""
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    conf = json.loads(c.pub["homeassistant/number/test_node/soc_min/config"])
    assert conf["object_id"] == "tst_soc_min"
    assert conf["default_entity_id"] == "number.test_car_soc_min"


def _configure_with(monkeypatch, device_name, sensor_name):
    cat = types.SimpleNamespace(**vars(_FAKE_CATALOG))
    cat.DEVICE = {**_FAKE_CATALOG.DEVICE, "name": device_name}
    cat.SENSORS = {"tst_x": (sensor_name, None, None, None)}
    cat.OPTIONAL_ENDPOINTS = {}
    mqtt.configure(cat)


# Expected values are python-slugify 9.0.0's output (the version HA pins) for the same text.
@pytest.mark.parametrize("device_name,name,expected", [
    ("R5", "SOC Max Target", "r5_soc_max_target"),            # the id r5 issue #83 lost
    ("Test Car", "Driver's Door", "test_car_driver_s_door"),   # apostrophe becomes a separator
    ("Test Car", "A  --  B", "test_car_a_b"),                  # runs of spaces/punctuation collapse
    ("Mixed CaSe", "SoC Max", "mixed_case_soc_max"),           # lower-cased
    ("Alpine A290", "Range 2", "alpine_a290_range_2"),         # digits kept
    ("Test Car", "Trailing!!!", "test_car_trailing"),          # trailing punctuation stripped
    ("  Test Car", "x", "test_car_x"),                         # leading separators stripped
    ("Test Car", "1,000 km", "test_car_1000_km"),              # a comma between digits is dropped
    ("Test Car", "a,b", "test_car_a_b"),                       # ...but not between letters
    ("Test Car", "under_score", "test_car_under_score"),
    ("Test Car", 'Quote "x"', "test_car_quote_x"),
])
def test_default_entity_id_slug_matches_ha(monkeypatch, device_name, name, expected):
    _configure_with(monkeypatch, device_name, name)
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    conf = json.loads(c.pub["homeassistant/sensor/test_node/tst_x/config"])
    assert conf["default_entity_id"] == f"sensor.{expected}"


@pytest.mark.parametrize("device_name,table,name", [
    ("Test Café", None, None),                   # non-ASCII device name
    ("Test Car", "SENSORS", "Température"),      # non-ASCII entity name, each table
    ("Test Car", "BINARY_SENSORS", "Prise ⚡"),
    ("Test Car", "ACTION_BUTTONS", "Klaxon ’"),
    ("Test Car", "NUMBERS", "Charge ≥"),
    ("Test Car", "SENSORS", "Tom &amp; Jerry"),  # HA decodes character references first
    ("Test Car", "SENSORS", "Driver&#39;s"),
    ("!!!", "SENSORS", "???"),                   # slugs to nothing: HA would say "unknown"
])
def test_configure_rejects_a_name_whose_entity_id_it_cannot_derive(monkeypatch, device_name, table, name):
    """HA transliterates non-ASCII with whichever unidecode backend is installed, and decodes HTML
    character references before slugging. Neither is reproduced here, so such a name must stop the
    add-on at startup rather than publish an id that silently differs from HA's."""
    cat = types.SimpleNamespace(**vars(_FAKE_CATALOG))
    cat.DEVICE = {**_FAKE_CATALOG.DEVICE, "name": device_name}
    if table:
        entry = next(iter(getattr(_FAKE_CATALOG, table).items()))
        setattr(cat, table, {entry[0]: (name, *entry[1][1:])})
    with pytest.raises(ValueError, match="entity id"):
        mqtt.configure(cat)


# --------------------------------------------------------------------------- #
# client wiring + callbacks
# --------------------------------------------------------------------------- #
class _FakePaho:
    def __init__(self):
        self.calls = {}

    def username_pw_set(self, u, p):
        self.calls["auth"] = (u, p)

    def will_set(self, topic, payload, retain=False):
        self.calls["will"] = (topic, payload)

    def reconnect_delay_set(self, min_delay, max_delay):
        self.calls["backoff"] = (min_delay, max_delay)

    def connect(self, host, port, keepalive):
        self.calls["connect"] = (host, port, keepalive)

    def loop_start(self):
        self.calls["loop_start"] = True

    def subscribe(self, topic):
        self.calls.setdefault("subs", []).append(topic)

    def publish(self, topic, payload, retain=False):
        self.calls.setdefault("pub", {})[topic] = payload


def _patch_paho(monkeypatch):
    fake = _FakePaho()
    monkeypatch.setattr(mqtt.paho_mqtt, "Client", lambda *a, **k: fake)
    return fake


def test_mqtt_connect_wires_client_with_keepalive_and_auth(monkeypatch):
    fake = _patch_paho(monkeypatch)
    monkeypatch.setenv("MQTT_USER", "u")
    monkeypatch.setenv("MQTT_PASS", "p")
    monkeypatch.setenv("MQTT_HOST", "broker")
    monkeypatch.setenv("MQTT_PORT", "1884")
    client = mqtt.mqtt_connect()
    assert client is fake
    assert fake.calls["auth"] == ("u", "p")
    assert fake.calls["connect"] == ("broker", 1884, 45)     # keepalive from the fake catalog
    assert fake.calls["will"][0] == "test_node/availability"
    assert fake.calls["loop_start"] is True


def test_mqtt_connect_without_auth(monkeypatch):
    fake = _patch_paho(monkeypatch)
    monkeypatch.delenv("MQTT_USER", raising=False)
    monkeypatch.setenv("MQTT_HOST", "broker")
    mqtt.mqtt_connect()
    assert "auth" not in fake.calls                          # no username -> no auth call


def test_on_message_dispatches_command(monkeypatch):
    scheduled = {}
    monkeypatch.setattr(mqtt, "_LOOP", object())

    async def handler(cmd, payload):
        return None

    monkeypatch.setattr(mqtt, "_COMMAND_HANDLER", handler)
    monkeypatch.setattr(mqtt.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: scheduled.update(cmd=coro) or coro.close())
    msg = types.SimpleNamespace(topic="test_node/cmd/wake", payload=b"go")
    mqtt._on_message(None, None, msg)
    assert "cmd" in scheduled


def test_on_message_ignores_non_command_and_unwired(monkeypatch):
    calls = []
    monkeypatch.setattr(mqtt.asyncio, "run_coroutine_threadsafe", lambda *a: calls.append(a))
    # not wired -> ignored
    mqtt._on_message(None, None, types.SimpleNamespace(topic="test_node/cmd/wake", payload=b""))
    # wired but wrong topic -> ignored
    monkeypatch.setattr(mqtt, "_LOOP", object())
    monkeypatch.setattr(mqtt, "_COMMAND_HANDLER", lambda c, p: None)
    mqtt._on_message(None, None, types.SimpleNamespace(topic="other/topic", payload=b""))
    assert calls == []


# --------------------------------------------------------------------------- #
# RFC 0009 / C2: re-publish discovery on HA's birth message, and tombstone
# retired ids as a minimal disabled config followed by the empty payload.
# --------------------------------------------------------------------------- #
def test_on_message_republishes_discovery_on_ha_birth():
    mqtt._MQTT_CTX["supported"], mqtt._MQTT_CTX["dist_unit"] = _ALL_EPS, "km"
    c = StubClient()
    mqtt._on_message(c, None, types.SimpleNamespace(topic="homeassistant/status", payload=b"online"))
    assert "homeassistant/sensor/test_node/tst_battery/config" in c.pub


def test_on_message_ignores_non_online_birth_payload():
    mqtt._MQTT_CTX["supported"], mqtt._MQTT_CTX["dist_unit"] = _ALL_EPS, "km"
    c = StubClient()
    mqtt._on_message(c, None, types.SimpleNamespace(topic="homeassistant/status", payload=b"offline"))
    assert c.pub == {}


def test_on_message_birth_is_a_noop_before_first_discovery():
    mqtt._MQTT_CTX["supported"] = None
    c = StubClient()
    mqtt._on_message(c, None, types.SimpleNamespace(topic="homeassistant/status", payload=b"online"))
    assert c.pub == {}


def test_on_connect_republishes_discovery_when_ctx_set():
    c = StubClient()
    mqtt._MQTT_CTX["supported"], mqtt._MQTT_CTX["dist_unit"] = _ALL_EPS, "km"
    mqtt._on_connect(c, None, None, 0)
    assert "test_node/cmd/#" in c.subs
    assert c.pub["test_node/availability"] == "online"
    assert "homeassistant/sensor/test_node/tst_battery/config" in c.pub   # discovery republished


def test_on_connect_subscribes_to_ha_birth_topic():
    """RFC 0009 option C/C2: HA's documented discovery trigger is its birth message on
    homeassistant/status, not only the add-on's own reconnect."""
    c = StubClient()
    mqtt._MQTT_CTX["supported"] = None
    mqtt._on_connect(c, None, None, 0)
    assert "homeassistant/status" in c.subs


def test_on_connect_skips_discovery_when_ctx_unset():
    c = StubClient()
    mqtt._MQTT_CTX["supported"] = None
    mqtt._on_connect(c, None, None, 0)
    assert "test_node/cmd/#" in c.subs and c.pub["test_node/availability"] == "online"
    assert not any(t.startswith("homeassistant/") for t in c.pub)         # no discovery


def test_on_connect_refused_does_not_subscribe_or_announce(caplog):
    import logging
    c = StubClient()
    mqtt._MQTT_CTX["supported"] = _ALL_EPS
    with caplog.at_level(logging.WARNING, logger="renault_mqtt.mqtt"):
        mqtt._on_connect(c, None, None, 5)   # refused CONNACK -> guard fires
    assert c.subs == [] and c.pub == {}                      # no subscribe / no online / no discovery
    assert any("refused" in r.message for r in caplog.records)


def test_on_disconnect_warns_only_on_error(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="renault_mqtt.mqtt"):
        mqtt._on_disconnect(None, None, None, 0)
        assert not caplog.records
        mqtt._on_disconnect(None, None, None, 1)
        assert any("disconnected" in r.message for r in caplog.records)


def test_data_gated_sensor_availability_follows_its_key():
    """A sensor whose endpoint can stop answering must go UNAVAILABLE, not blank.

    The A290's hvac-settings is advertised as supported and then returns 502000 forever, so the
    poller trips a breaker and stops writing the key. Before this, the two climate sensors it
    feeds rendered as EMPTY STRINGS — indistinguishable from "the car reported nothing" — which
    is what the add-on backlog asked to fix and what three releases of breaker work left behind.
    """
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    conf = json.loads(c.pub["homeassistant/sensor/test_node/tst_gated/config"])

    # HA forbids mixing availability_topic with an availability list; the list must replace it.
    assert "availability_topic" not in conf
    assert conf["availability_mode"] == "all"

    topics = [a["topic"] for a in conf["availability"]]
    assert "test_node/availability" in topics      # the add-on's own online/offline still counts
    assert "test_node/state" in topics             # ...AND the key-presence test

    tmpl = next(a["value_template"] for a in conf["availability"] if a["topic"] == "test_node/state")
    assert "value_json.gated is defined" in tmpl   # prefix stripped, same as value_template
    assert "online" in tmpl and "offline" in tmpl

    # An ordinary sensor is untouched — this must not silently change every entity's availability.
    plain = json.loads(c.pub["homeassistant/sensor/test_node/tst_plain/config"])
    assert plain["availability_topic"] == "test_node/availability"
    assert "availability" not in plain


def test_data_gating_is_optional_for_a_catalog_that_omits_it():
    """The r5 catalog may not declare DATA_GATED_SENSORS; discovery must not blow up."""
    import types
    cat = mqtt._CAT
    stripped = types.SimpleNamespace(**{k: v for k, v in vars(cat).items()
                                        if k != "DATA_GATED_SENSORS"})
    mqtt._CAT = stripped
    try:
        c = StubClient()
        mqtt.publish_discovery(c, _ALL_EPS, "km")
        conf = json.loads(c.pub["homeassistant/sensor/test_node/tst_gated/config"])
        assert conf["availability_topic"] == "test_node/availability"   # falls back cleanly
    finally:
        mqtt._CAT = cat


# --------------------------------------------------------------------------- #
# retiring binary_sensor / button / number discovery
# --------------------------------------------------------------------------- #
# (domain, live table, retired list, an id the fake catalog publishes live in that domain)
_RETIRABLE = [
    ("sensor", "SENSORS", "RETIRED_SENSORS", "tst_battery"),
    ("binary_sensor", "BINARY_SENSORS", "RETIRED_BINARY_SENSORS", "tst_plug"),
    ("button", "ACTION_BUTTONS", "RETIRED_BUTTONS", "tst_wake"),
    ("number", "NUMBERS", "RETIRED_NUMBERS", "tst_soc_min"),
]
_NEW_RETIRABLE = _RETIRABLE[1:]


def _catalog(**overrides):
    return types.SimpleNamespace(**{**vars(_FAKE_CATALOG), **overrides})


def _live_topic(obj):
    """The topic that carried `obj`'s live config, read off a real publish rather than re-derived,
    so a tombstone derived differently (full id vs prefix-stripped) cannot agree with it by accident."""
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    return next(t for t, p, _ in c.log
                if t.startswith("homeassistant/") and p and json.loads(p).get("object_id") == obj)


def _retire(table, attr, obj):
    """The fake catalog with `obj` moved out of its live table into `attr`, as a rename does."""
    return _catalog(**{table: {k: v for k, v in getattr(_FAKE_CATALOG, table).items() if k != obj},
                       attr: [obj]})


@pytest.mark.parametrize("domain,table,attr,obj", _NEW_RETIRABLE)
def test_retired_id_is_tombstoned_on_the_topic_it_was_published_on(domain, table, attr, obj):
    live = _live_topic(obj)
    assert f"/{domain}/" in live
    mqtt.configure(_retire(table, attr, obj))
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    # The final write is a zero-length RETAINED payload: that is what makes HA delete the entity.
    # (Since RFC 0009/C2, it is preceded by an unretained minimal disabled config -- see
    # _assert_minimal_then_tombstone -- which is what lets the tombstone reach an id HA's current
    # run never discovered, e.g. after an offline restart.)
    assert c.writes(live)[-1] == ("", True)


def test_retired_tombstones_precede_every_live_config():
    """A rename tombstones the old unique_id and publishes the new one in the same pass. Whether HA
    hands the freed entity_id to the new entity may depend on the old one being gone first."""
    mqtt.configure(_catalog(**{attr: [f"tst_gone_{domain}"] for domain, _, attr, _ in _RETIRABLE}))
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    tomb_topics = {
        "homeassistant/sensor/test_node/tst_gone_sensor/config",
        "homeassistant/binary_sensor/test_node/tst_gone_binary_sensor/config",
        "homeassistant/button/test_node/gone_button/config",
        "homeassistant/number/test_node/gone_number/config"}
    tombs = [c.index_of(t, "") for t in tomb_topics]
    # A retirement's own minimal config is also a non-empty write on its topic, so exclude those
    # topics when looking for the first REAL live config.
    first_live = next(i for i, (t, p, _) in enumerate(c.log)
                       if t.startswith("homeassistant/") and p and t not in tomb_topics)
    assert max(tombs) < first_live


@pytest.mark.parametrize("domain,table,attr,obj", _RETIRABLE)
def test_retired_id_that_is_still_live_is_rejected(domain, table, attr, obj):
    with pytest.raises(ValueError, match=obj.removeprefix("tst_")):
        mqtt.configure(_catalog(**{attr: [obj]}))


@pytest.mark.parametrize("domain,table,attr,obj", [r for r in _RETIRABLE if r[0] in ("button", "number")])
def test_retired_id_clashing_on_the_stripped_topic_is_rejected(domain, table, attr, obj):
    # Buttons and numbers are keyed by the stripped id, so "wake" and "tst_wake" share one topic.
    with pytest.raises(ValueError):
        mqtt.configure(_catalog(**{attr: [obj.removeprefix("tst_")]}))


def test_an_id_retired_in_one_domain_may_be_live_in_another():
    """Both add-ons ship this: soc_min moved from SENSORS to NUMBERS, so its sensor config is
    retired while the number is live. Different topics, so it must not trip the guard."""
    mqtt.configure(_catalog(RETIRED_SENSORS=["tst_soc_min"]))
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    assert c.writes("homeassistant/sensor/test_node/tst_soc_min/config")[-1] == ("", True)
    assert json.loads(c.pub["homeassistant/number/test_node/soc_min/config"])["object_id"] == "tst_soc_min"


def test_retired_ids_are_named_in_the_log(caplog):
    import logging
    mqtt.configure(_catalog(**{attr: [f"tst_gone_{domain}"] for domain, _, attr, _ in _NEW_RETIRABLE}))
    with caplog.at_level(logging.DEBUG, logger="renault_mqtt.mqtt"):
        mqtt.publish_discovery(StubClient(), _ALL_EPS, "km")
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    for domain, _, _, _ in _NEW_RETIRABLE:
        assert any(f"tst_gone_{domain}" in m and f"{domain} " in m for m in debug), domain


def _assert_minimal_then_tombstone(writes, obj, required_topic_key, required_topic_value):
    """C2's shape for a retirement: an unretained minimal config (unique_id + device + the domain's
    required topic + enabled_by_default: false, so HA never records history/logbook for it and it
    is removed even when the install has disabled it), then the ordinary empty retained tombstone."""
    assert len(writes) == 2, writes
    (minimal_payload, minimal_retain), (tomb_payload, tomb_retain) = writes
    assert (tomb_payload, tomb_retain) == ("", True)
    assert minimal_retain is False
    minimal = json.loads(minimal_payload)
    assert minimal["unique_id"] == obj
    assert minimal["device"] == mqtt.DEVICE
    assert minimal["enabled_by_default"] is False
    assert minimal[required_topic_key] == required_topic_value


@pytest.mark.parametrize("domain,table,attr,obj", _NEW_RETIRABLE)
def test_retired_id_gets_a_minimal_disabled_config_before_the_tombstone(domain, table, attr, obj):
    """Gate result (RFC 0009, 2026-09-26): the minimal config alone (without
    enabled_by_default: false) wrote 2 recorder history rows per id per cycle for an id the
    install never had, and left a disabled orphan un-removed. Adding enabled_by_default: false
    (option C2) measured clean on both."""
    live = _live_topic(obj)
    mqtt.configure(_retire(table, attr, obj))
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    # command_topic, not state_topic, for button and number: HA's mqtt.number schema requires
    # command_topic, so a minimal config carrying only state_topic fails validation and is never
    # discovered -- an e2e gap that unit tests with a StubClient cannot see (real-HA finding,
    # RFC 0009 gate, 2026-09-26).
    key, value = (("command_topic", f"test_node/cmd/{obj.removeprefix('tst_')}") if domain in ("button", "number")
                  else ("state_topic", mqtt.STATE_TOPIC))
    _assert_minimal_then_tombstone(c.writes(live), obj, key, value)


def test_retired_sensor_gets_a_minimal_disabled_config_before_the_tombstone():
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    _assert_minimal_then_tombstone(c.writes("homeassistant/sensor/test_node/tst_old/config"),
                                    "tst_old", "state_topic", mqtt.STATE_TOPIC)


def test_unsupported_endpoint_sensor_gets_a_minimal_disabled_config_before_the_tombstone():
    """The proposal covers every retirement, not only RETIRED_SENSORS: an endpoint the car does
    not support is tombstoned the same way, so an orphan from a lost endpoint doesn't linger
    either (option D would skip this and leave that gap, per the RFC's cost/risk table)."""
    c = StubClient()
    mqtt.publish_discovery(c, set(), "km")           # nothing supported -> pressure cleared
    _assert_minimal_then_tombstone(c.writes("homeassistant/sensor/test_node/tst_pressure/config"),
                                    "tst_pressure", "state_topic", mqtt.STATE_TOPIC)


def test_new_retired_lists_are_optional():
    """a290's catalog sets none of them: discovery must publish no extra tombstones."""
    assert not any(hasattr(_FAKE_CATALOG, attr) for _, _, attr, _ in _NEW_RETIRABLE)
    c = StubClient()
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    cleared = sorted(t for t, p, _ in c.log if p == "" and t.startswith("homeassistant/"))
    assert cleared == ["homeassistant/button/test_node/forbidden/config",
                       "homeassistant/button/test_node/refresh/config",
                       "homeassistant/sensor/test_node/tst_old/config"]
