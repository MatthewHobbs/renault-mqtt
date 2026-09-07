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
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    wake = json.loads(c.pub[f"{base}/wake/config"])
    assert wake["command_topic"] == "test_node/cmd/wake" and wake["object_id"] == "tst_wake"
    assert json.loads(c.pub[f"{base}/refresh/config"])["name"] == "Refresh"   # location on -> shown
    assert c.pub[f"{base}/forbidden/config"] == ""                            # unsupported -> cleared
    # location off suppresses the refresh-location button too
    c = StubClient()
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", False)
    mqtt.publish_discovery(c, _ALL_EPS, "km")
    assert c.pub[f"{base}/refresh/config"] == ""


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


def test_on_connect_republishes_discovery_when_ctx_set():
    c = StubClient()
    mqtt._MQTT_CTX["supported"], mqtt._MQTT_CTX["dist_unit"] = _ALL_EPS, "km"
    mqtt._on_connect(c, None, None, 0)
    assert "test_node/cmd/#" in c.subs
    assert c.pub["test_node/availability"] == "online"
    assert "homeassistant/sensor/test_node/tst_battery/config" in c.pub   # discovery republished


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
