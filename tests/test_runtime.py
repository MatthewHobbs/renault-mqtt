"""Unit tests for renault_mqtt.runtime (account resolution, health/panel server, auth classification, and main loop)."""
import asyncio
import json
import logging
import sys
import types

import pytest

from renault_mqtt import config, freshness, mqtt
from renault_mqtt.runtime import (
    HEALTH_GRACE,
    HEALTH_PORT,
    MILES_LOCALES,
    is_auth_failure,
    panel_page,
    panel_state,
    resolve_account,
    run_main,
    start_health_server,
)
from renault_mqtt.util import iso

LOG = logging.getLogger("renault_mqtt.test_runtime")


@pytest.fixture(autouse=True)
def _env_prefix(monkeypatch):
    monkeypatch.setattr(config, "ENV_PREFIX", "TEST_")
    monkeypatch.setattr(config, "_DISCOVERED_ACCOUNT_ID", None)
    saved_ctx = dict(mqtt._MQTT_CTX)
    yield
    mqtt._MQTT_CTX.clear()
    mqtt._MQTT_CTX.update(saved_ctx)


def _bound_port(runner):
    return runner.addresses[0][1]


# --------------------------------------------------------------------------- #
# resolve_account
# --------------------------------------------------------------------------- #
def test_resolve_account_from_env(monkeypatch):
    monkeypatch.setenv("TEST_ACCOUNT_ID", "acct-configured")
    assert asyncio.run(resolve_account(object(), log=LOG)) == "acct-configured"
    assert config._DISCOVERED_ACCOUNT_ID is None


def test_resolve_account_matches_vin_garage_case_insensitively(monkeypatch, caplog):
    monkeypatch.delenv("TEST_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("TEST_VIN", "vf1stubvin0000001")
    garages = {
        "acct-renault": [types.SimpleNamespace(vin="VF1OTHER")],
        "acct-alpine": [types.SimpleNamespace(vin="VF1STUBVIN0000001")],
    }

    class Client:
        async def get_person(self):
            return types.SimpleNamespace(accounts=[
                types.SimpleNamespace(accountType="MYRENAULT", accountId="acct-renault"),
                types.SimpleNamespace(accountType="MYALPINE", accountId="acct-alpine"),
            ])

        async def get_api_account(self, aid):
            return types.SimpleNamespace(
                get_vehicles=lambda: asyncio.sleep(0, types.SimpleNamespace(vehicleLinks=garages[aid]))
            )

    with caplog.at_level(logging.INFO, logger=LOG.name):
        assert asyncio.run(resolve_account(Client(), log=LOG)) == "acct-alpine"
    assert config._DISCOVERED_ACCOUNT_ID == "acct-alpine"
    assert "acct-alpine" not in config.redact("boom /accounts/acct-alpine/vehicles/V")
    assert any("Auto-discovered MYALPINE account" in r.getMessage() for r in caplog.records)


def test_resolve_account_skips_failing_garage_and_continues(monkeypatch, caplog):
    monkeypatch.delenv("TEST_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("TEST_VIN", "VF1STUBVIN0000002")

    class Client:
        async def get_person(self):
            return types.SimpleNamespace(accounts=[
                types.SimpleNamespace(accountType="SFDC", accountId="acct-bad"),
                types.SimpleNamespace(accountType="MYRENAULT", accountId="acct-good"),
            ])

        async def get_api_account(self, aid):
            if aid == "acct-bad":
                raise RuntimeError("kamereon 500")
            return types.SimpleNamespace(
                get_vehicles=lambda: asyncio.sleep(
                    0, types.SimpleNamespace(vehicleLinks=[types.SimpleNamespace(vin="VF1STUBVIN0000002")])
                )
            )

    with caplog.at_level(logging.DEBUG, logger=LOG.name):
        assert asyncio.run(resolve_account(Client(), log=LOG)) == "acct-good"
    assert config._DISCOVERED_ACCOUNT_ID == "acct-good"
    assert any("Garage lookup for the SFDC account failed" in r.getMessage() for r in caplog.records)


def test_resolve_account_falls_back_to_first_myrenault_when_no_garage_matches(monkeypatch, caplog):
    monkeypatch.delenv("TEST_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("TEST_VIN", raising=False)

    class Client:
        async def get_person(self):
            return types.SimpleNamespace(accounts=[
                types.SimpleNamespace(accountType="OTHER", accountId="acct-other"),
                types.SimpleNamespace(accountType="MYRENAULT", accountId="acct-first"),
                types.SimpleNamespace(accountType="MYRENAULT", accountId="acct-second"),
            ])

    with caplog.at_level(logging.WARNING, logger=LOG.name):
        assert asyncio.run(resolve_account(Client(), log=LOG, prefix="TEST_")) == "acct-first"
    assert config._DISCOVERED_ACCOUNT_ID == "acct-first"
    assert any("falling back to the MYRENAULT account" in r.getMessage() for r in caplog.records)


def test_resolve_account_raises_when_no_match_and_no_myrenault(monkeypatch):
    monkeypatch.delenv("TEST_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("TEST_VIN", raising=False)

    class Client:
        async def get_person(self):
            return types.SimpleNamespace(accounts=[
                types.SimpleNamespace(accountType="OTHER", accountId="acct-x"),
            ])

    with pytest.raises(RuntimeError, match="TEST_ACCOUNT_ID is not set"):
        asyncio.run(resolve_account(Client(), log=LOG))


# --------------------------------------------------------------------------- #
# start_health_server / panel_page / panel_state
# --------------------------------------------------------------------------- #
def _make_fake_web():
    class Response:
        def __init__(self, *, text="", content_type="text/plain", status=200):
            self.status = status
            self.text = text
            self.content_type = content_type

    class FileResponse:
        def __init__(self, path):
            self.status = 200
            self.path = str(path)
            with open(path, encoding="utf-8") as fh:
                self.text = fh.read()

    class Router:
        def __init__(self):
            self.routes = {}

        def add_get(self, path, handler):
            self.routes[path] = handler

    class Application:
        def __init__(self):
            self.router = Router()

    class AppRunner:
        def __init__(self, app):
            self.app = app
            self.setup_called = False
            self.cleaned_up = False
            self.addresses = []

        async def setup(self):
            self.setup_called = True

        async def cleanup(self):
            self.cleaned_up = True

    class TCPSite:
        def __init__(self, runner, host, port):
            self.runner = runner
            self.host = host
            self.port = port

        async def start(self):
            self.runner.addresses.append((self.host, self.port or 54321))

    return types.SimpleNamespace(
        Response=Response,
        FileResponse=FileResponse,
        Application=Application,
        AppRunner=AppRunner,
        TCPSite=TCPSite,
        json_response=lambda data: Response(text=json.dumps(data), content_type="application/json"),
    )


def test_health_server_serves_healthz_panel_and_state(monkeypatch, tmp_path):
    assert HEALTH_PORT == 8099
    assert MILES_LOCALES == frozenset({"en_gb"})
    fake_web = _make_fake_web()
    fake_aiohttp = types.ModuleType("aiohttp")
    fake_aiohttp.web = fake_web
    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)
    monkeypatch.setitem(sys.modules, "aiohttp.web", fake_web)
    panel = tmp_path / "panel.html"
    panel.write_text("<html>Test Status Panel</html>", encoding="utf-8")
    latest = {"version": "1.0", "ok": True, "supported": ["a"], "data": {"battery_level": 42}}
    holder = {"path": str(panel)}

    async def _call_route(handler):
        res = handler(None)
        return await res if asyncio.iscoroutine(res) else res

    async def scenario():
        runner = await start_health_server(
            panel_file=lambda: holder["path"], latest=latest, log=LOG, port=0,
        )
        assert runner.setup_called is True
        assert _bound_port(runner) == 54321
        routes = runner.app.router.routes
        try:
            r_health = await _call_route(routes["/healthz"])
            assert r_health.status == 200 and r_health.text == "ok"
            r_state = await _call_route(routes["/api/state"])
            body = json.loads(r_state.text)
            assert r_state.status == 200 and body["ok"] is True and body["data"]["battery_level"] == 42
            r_page = await _call_route(routes["/"])
            assert r_page.status == 200 and "Test Status Panel" in r_page.text
            panel.unlink()
            r_missing = await _call_route(routes["/"])
            assert r_missing.status == 200 and r_missing.text == "Status panel unavailable"
            direct_page = await panel_page(str(panel))
            assert direct_page.text == "Status panel unavailable"
            direct_state = await panel_state(latest)
            assert json.loads(direct_state.text)["version"] == "1.0"
        finally:
            await runner.cleanup()
        assert runner.cleaned_up is True

    asyncio.run(scenario())


def test_healthz_answers_503_once_the_expected_progress_deadline_passes(monkeypatch):
    fake_web = _make_fake_web()
    fake_aiohttp = types.ModuleType("aiohttp")
    fake_aiohttp.web = fake_web
    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)
    monkeypatch.setitem(sys.modules, "aiohttp.web", fake_web)
    state = {"deadline": 100.0}
    clock = {"t": 99.999}

    async def _call_route(handler):
        res = handler(None)
        return await res if asyncio.iscoroutine(res) else res

    async def scenario():
        runner = await start_health_server(
            panel_file="unused.html", latest={}, log=LOG, port=0,
            health_state=state, now_monotonic=lambda: clock["t"],
        )
        routes = runner.app.router.routes
        try:
            before = await _call_route(routes["/healthz"])
            assert before.status == 200 and before.text == "ok"
            clock["t"] = 100.0
            stalled = await _call_route(routes["/healthz"])
            assert stalled.status == 503 and stalled.text == "stalled"
            # Progress resumes: the loop moves the deadline and the answer recovers.
            state["deadline"] = 160.0
            resumed = await _call_route(routes["/healthz"])
            assert resumed.status == 200 and resumed.text == "ok"
            # Startup (no deadline yet) is never reported as stalled.
            state["deadline"] = None
            clock["t"] = 10_000.0
            startup = await _call_route(routes["/healthz"])
            assert startup.status == 200 and startup.text == "ok"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# is_auth_failure
# --------------------------------------------------------------------------- #
class _FakeAuthErr(Exception):
    pass


class _FakeHttpErr(Exception):
    def __init__(self, status):
        super().__init__("http error")
        self.status = status


def test_is_auth_failure_classifies_types_statuses_and_keywords():
    assert is_auth_failure(_FakeAuthErr("opaque"), auth_exceptions=(_FakeAuthErr,)) is True
    assert is_auth_failure(_FakeHttpErr(401), http_error_type=_FakeHttpErr) is True
    assert is_auth_failure(_FakeHttpErr(403), http_error_type=_FakeHttpErr) is True
    assert is_auth_failure(_FakeHttpErr(500), http_error_type=_FakeHttpErr) is False
    for word in ("Invalid login", "bad password", "credential expired", "HTTP 401", "error 403"):
        assert is_auth_failure(RuntimeError(word)) is True
    assert is_auth_failure(RuntimeError("network unreachable")) is False


# --------------------------------------------------------------------------- #
# run_main orchestration
# --------------------------------------------------------------------------- #
class _FakeSnapshotsClient:
    def __init__(self):
        self.pubs = []
        self.snapshots = self
        self.connected = True
        self.latest = None
        self.location = None
        self.closed = False

    def update(self, data, **kwargs):
        self.latest = json.dumps(data)
        if not mqtt.PUBLISH_LOCATION:
            self.location = ""
        elif "location" in kwargs:
            self.location = json.dumps(kwargs["location"]) if kwargs["location"] else ""
        self.replay()

    def replay(self):
        if self.connected and self.latest is not None and not self.closed:
            self.publish(mqtt.STATE_TOPIC, self.latest, retain=True)
            if self.location is not None:
                self.publish(mqtt.ATTR_TOPIC, self.location, retain=True)
            self.publish(mqtt.AVAIL_TOPIC, "online", retain=True)

    def connect(self):
        self.connected = True
        mqtt.publish_discovery(self, mqtt._MQTT_CTX["supported"], mqtt._MQTT_CTX["dist_unit"])
        self.replay()

    async def close(self):
        self.closed = True
        self.publish(mqtt.AVAIL_TOPIC, "offline", retain=True)

    def publish(self, topic, payload="", retain=False):
        self.pubs.append((topic, payload, retain))


class _FakeVS:
    def __init__(self, locale):
        self.locale = locale
        self.invalidations = 0
        self.closed = False

    async def invalidate(self):
        self.invalidations += 1

    async def close(self):
        self.closed = True


def _wire_harness(monkeypatch, *, prefix="TEST_", locale="en_GB", interval="60", stale_hours="6"):
    for k, v in {
        f"{prefix}USERNAME": "u",
        f"{prefix}PASSWORD": "p",
        f"{prefix}VIN": "SECRETVIN999",
        "MQTT_HOST": "broker",
        f"{prefix}LOCALE": locale,
        f"{prefix}POLL_INTERVAL": interval,
        f"{prefix}STALE_HOURS": stale_hours,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(config, "ENV_PREFIX", prefix)
    monkeypatch.setattr(mqtt, "STATE_TOPIC", "test/state")
    monkeypatch.setattr(mqtt, "ATTR_TOPIC", "test/attr")
    monkeypatch.setattr(mqtt, "AVAIL_TOPIC", "test/avail")
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", True)
    monkeypatch.setattr(mqtt, "ENABLE_REFRESH_LOCATION", False)
    fc = _FakeSnapshotsClient()
    discoveries = []

    def fake_connect(*, snapshots=False):
        assert snapshots is True
        return fc

    monkeypatch.setattr(mqtt, "mqtt_connect", fake_connect)
    monkeypatch.setattr(mqtt, "publish_discovery", lambda c, sup, du: discoveries.append((set(sup), du)))
    return fc, discoveries


def test_run_main_exits_on_missing_required_config(monkeypatch):
    for k in ("TEST_USERNAME", "TEST_PASSWORD", "TEST_VIN", "MQTT_HOST"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(run_main(
            addon_name="Test", version="1.0", setup_logging=lambda: None,
            run_command=lambda *a: None, start_health_server=lambda **kw: asyncio.sleep(0),
            load_state=dict, save_state=lambda s: None, latest={},
            vsession_factory=_FakeVS, support_endpoints={"ep"},
            detect_supported=lambda vs, supported=None, unresolved=None: asyncio.sleep(0, set()),
            run_deploy=lambda **kw: asyncio.sleep(0), poll_once=lambda *a: asyncio.sleep(0),
            api_timeout_total=60, log=LOG,
        ))
    assert exc.value.code == 1


@pytest.mark.parametrize("early_signals,log_duration,locale,expect_unit,plug_key,ts_key", [
    (False, False, "en_GB", "mi", "plug_status", "last_updated"),
    (True, True, "fr_FR", "km", "charger_plug_status", "battery_last_activity"),
])
def test_run_main_success_cycle_support_retry_and_shutdown(
    monkeypatch, caplog, early_signals, log_duration, locale, expect_unit, plug_key, ts_key,
):
    fc, discoveries = _wire_harness(monkeypatch, locale=locale)
    clock = {"t": 1767225600.0, "m": 10.0}
    monkeypatch.setattr(freshness, "now_ts", lambda: clock["t"])
    order, saved_states, deploy_calls, vs_list = [], [], [], []
    captured = {}
    latest = {"version": "1.0", "ok": False, "last_poll": None, "supported": [],
              "dist_unit": "km", "data": {"stale": 1}}

    async def start_health(health_state=None):
        captured["health_state"] = health_state
        order.append("health")
        return types.SimpleNamespace(cleanup=lambda: asyncio.sleep(0, order.append("health_cleanup")))

    async def detect(vs, supported=None, unresolved=None):
        if not supported:
            order.append("detect_initial")
            supported.add("pressure")
            return
        order.append("detect_retry")
        supported.add("soc-levels")
        unresolved.clear()
        return supported

    async def deploy(**kw):
        order.append("deploy")
        deploy_calls.append(kw)

    async def poll(vs, state, cap, sup, du):
        order.append("poll")
        clock["m"] += 1.5
        return ({
            "battery_level": 80,
            plug_key: "Connected",
            "charging": "on",
            "plug_suspect": "off",
            ts_key: iso(clock["t"] - 600),
        }, {"latitude": 51.5, "longitude": -0.1})

    class OneCycleStop:
        def __init__(self):
            order.append("stop_created")
            self.checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks > 1

        def set(self):
            pass

        async def wait(self):
            raise asyncio.TimeoutError

    def make_vs(loc):
        vs = _FakeVS(loc)
        vs_list.append(vs)
        return vs

    with caplog.at_level(logging.INFO, logger=LOG.name):
        asyncio.run(run_main(
            addon_name="Test Car", version="1.0", setup_logging=lambda: order.append("logging"),
            run_command="handler", start_health_server=start_health,
            load_state=lambda: {}, save_state=lambda s: saved_states.append(dict(s)),
            latest=latest, vsession_factory=make_vs,
            support_endpoints={"pressure", "soc-levels"}, detect_supported=detect,
            run_deploy=deploy, poll_once=poll, api_timeout_total=60, log=LOG,
            plug_key=plug_key, last_updated_key=ts_key,
            log_duration=log_duration, early_signals=early_signals,
            now=lambda: clock["t"], monotonic=lambda: clock["m"],
            event_factory=OneCycleStop,
        ))

    if early_signals:
        assert order[:5] == ["logging", "stop_created", "health", "detect_initial", "deploy"]
    else:
        assert order[:5] == ["logging", "health", "detect_initial", "deploy", "stop_created"]
    assert deploy_calls == [{"refresh_location": False}]
    assert discoveries == [({"pressure", "soc-levels"}, expect_unit)]
    assert latest["ok"] is True
    # interval 60 in the harness: wait + poll budget + grace from the iteration's own clock.
    assert captured["health_state"]["deadline"] == (
        clock["m"] + 60 + max(30, 60 - 10) + min(60, max(30, 60 - 10)) + HEALTH_GRACE)
    assert latest["dist_unit"] == expect_unit
    assert latest["supported"] == ["pressure", "soc-levels"]
    assert latest["data"]["data_stale"] == "off"
    assert latest["data"]["poll_failing"] == "off"
    assert latest["data"]["api_auth_failure"] == "off"
    assert saved_states and saved_states[-1]["last_success"] == clock["t"]
    assert vs_list[0].closed is True and fc.closed is True
    messages = [r.getMessage() for r in caplog.records]
    if log_duration:
        assert any("Published in 1.5s: 80% battery, plug=Connected" in m for m in messages)
    else:
        assert any("Published: 80% battery, plug=Connected" in m for m in messages)


def test_run_main_bounds_startup_detection_and_keeps_polling(monkeypatch, caplog):
    _wire_harness(monkeypatch)
    latest = {"version": "1.0", "ok": False, "last_poll": None, "supported": []}
    order, timeouts = [], []
    calls = {"n": 0}

    async def detect(vs, supported=None, unresolved=None):
        order.append("detect")
        return {"pressure"}

    async def poll(vs, state, cap, sup, du):
        order.append("poll")
        return ({"battery_level": 50}, None)

    async def wait_for(coro, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            timeouts.append(timeout)
            coro.close()
            raise asyncio.TimeoutError
        return await coro

    class OneCycleStop:
        def __init__(self):
            self.checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks > 1

        def set(self):
            pass

        async def wait(self):
            return None

    with caplog.at_level(logging.WARNING, logger=LOG.name):
        asyncio.run(run_main(
            addon_name="Test Car", version="1.0", setup_logging=lambda: None,
            run_command="handler", start_health_server=lambda **kw: asyncio.sleep(
                0, types.SimpleNamespace(cleanup=lambda: asyncio.sleep(0))),
            load_state=dict, save_state=lambda s: None, latest=latest,
            vsession_factory=_FakeVS, support_endpoints={"pressure"},
            detect_supported=detect, run_deploy=lambda **kw: asyncio.sleep(0),
            poll_once=poll, api_timeout_total=60, log=LOG, wait_for=wait_for,
            event_factory=OneCycleStop,
        ))

    assert timeouts == [max(30, 60 - 10)]
    # The first poll still runs; the unresolved retry then asks for support again.
    assert order == ["poll", "detect"]
    assert latest["ok"] is True and latest["supported"] == []
    assert any("Endpoint detection timed out" in r.getMessage() for r in caplog.records)


def test_run_main_support_retry_failure_is_logged_and_non_fatal(monkeypatch, caplog):
    fc, discoveries = _wire_harness(monkeypatch)
    clock = {"t": 1767225600.0}
    monkeypatch.setattr(freshness, "now_ts", lambda: clock["t"])
    latest = {"version": "1.0", "ok": False, "last_poll": None, "supported": [], "dist_unit": "km", "data": {}}

    async def detect(vs, supported=None, unresolved=None):
        if supported == set():
            supported.add("pressure")
            return
        raise RuntimeError("retry failed for SECRETVIN999")

    async def poll(vs, state, cap, sup, du):
        return ({"battery_level": 55, "last_updated": iso(clock["t"])}, None)

    class OneShot:
        def __init__(self):
            self.n = 0

        def is_set(self):
            self.n += 1
            return self.n > 1

        def set(self):
            pass

        async def wait(self):
            raise asyncio.TimeoutError

    with caplog.at_level(logging.WARNING, logger=LOG.name):
        asyncio.run(run_main(
            addon_name="Test", version="1.0", setup_logging=lambda: None,
            run_command="h", start_health_server=lambda **kw: asyncio.sleep(0, types.SimpleNamespace(cleanup=lambda: asyncio.sleep(0))),
            load_state=dict, save_state=lambda s: None, latest=latest,
            vsession_factory=_FakeVS, support_endpoints={"pressure", "soc-levels"},
            detect_supported=detect, run_deploy=lambda **kw: asyncio.sleep(0),
            poll_once=poll, api_timeout_total=60, log=LOG,
            now=lambda: clock["t"], event_factory=OneShot,
        ))

    assert discoveries == []
    assert latest["ok"] is True and latest["data"]["battery_level"] == 55
    assert any("Endpoint-support retry failed: retry failed for ***" in r.getMessage() for r in caplog.records)


def test_run_main_failure_backoff_relogin_cadence_and_redaction(monkeypatch):
    fc, _ = _wire_harness(monkeypatch, interval="300")
    clock = {"t": 1767225600.0}
    monkeypatch.setattr(freshness, "now_ts", lambda: clock["t"])
    latest = {"version": "1.0", "ok": True, "last_poll": None, "supported": [], "dist_unit": "km",
              "data": {"stale_seed": 99}}
    vs_holder, delays, saved = {}, [], []
    polls = {"n": 0}

    def make_vs(locale):
        vs = _FakeVS(locale)
        vs.marks = []

        async def inv():
            vs.marks.append(polls["n"])

        vs.invalidate = inv
        vs_holder["vs"] = vs
        return vs

    async def poll(vs, state, cap, sup, du):
        polls["n"] += 1
        if polls["n"] == 4:
            raise _FakeAuthErr("auth failed for SECRETVIN999")
        raise RuntimeError("transient down for SECRETVIN999")

    class StopFive:
        def is_set(self):
            return polls["n"] >= 5

        def set(self):
            pass

        async def wait(self):
            pass

    real_wait_for = asyncio.wait_for

    async def fake_wait_for(aw, timeout):
        if getattr(aw, "__qualname__", "").endswith("StopFive.wait"):
            delays.append(timeout)
            aw.close()
            raise asyncio.TimeoutError
        return await real_wait_for(aw, timeout)

    asyncio.run(run_main(
        addon_name="Test", version="1.0", setup_logging=lambda: None,
        run_command="h", start_health_server=lambda **kw: asyncio.sleep(0, types.SimpleNamespace(cleanup=lambda: asyncio.sleep(0))),
        load_state=dict, save_state=lambda s: saved.append(dict(s)), latest=latest,
        vsession_factory=make_vs, support_endpoints=set(),
        detect_supported=lambda vs, supported=None, unresolved=None: asyncio.sleep(0, set()),
        run_deploy=lambda **kw: asyncio.sleep(0), poll_once=poll, api_timeout_total=60,
        log=LOG, auth_exceptions=(_FakeAuthErr,), now=lambda: clock["t"],
        event_factory=StopFive, wait_for=fake_wait_for,
    ))

    # 3rd transient failure invalidates; 4th is auth failure so it invalidates too; 5th does not.
    assert vs_holder["vs"].marks == [3, 4]
    assert delays == [300, 600, 1200, 1800, 1800]
    assert len(saved) == 5
    assert "SECRETVIN999" not in latest.get("error", "")
    assert "***" in latest.get("error", "")
    assert "stale_seed" not in latest["data"]
    assert latest["data"]["poll_failing"] == "on"


def test_run_main_signal_during_startup_skips_poll_loop_When_early_signals_enabled(monkeypatch):
    fc, _ = _wire_harness(monkeypatch)
    latest = {"version": "1.0", "ok": False, "last_poll": None, "supported": [], "dist_unit": "km", "data": {}}
    polled = []

    class PreSetStop:
        def is_set(self):
            return True

        def set(self):
            pass

        async def wait(self):
            pass

    asyncio.run(run_main(
        addon_name="Test", version="1.0", setup_logging=lambda: None,
        run_command="h", start_health_server=lambda **kw: asyncio.sleep(0, types.SimpleNamespace(cleanup=lambda: asyncio.sleep(0))),
        load_state=dict, save_state=lambda s: None, latest=latest,
        vsession_factory=_FakeVS, support_endpoints=set(),
        detect_supported=lambda vs, supported=None, unresolved=None: asyncio.sleep(0, set()),
        run_deploy=lambda **kw: asyncio.sleep(0),
        poll_once=lambda *a: asyncio.sleep(0, polled.append(1)),
        api_timeout_total=60, log=LOG, early_signals=True, event_factory=PreSetStop,
    ))
    assert polled == []


def test_run_main_keeps_partial_startup_support_after_timeout(monkeypatch):
    _wire_harness(monkeypatch)
    latest = {"version": "1.0", "ok": False, "last_poll": None, "supported": []}
    initial = {"hvac-settings"}

    async def detect(vs, supported=None, unresolved=None):
        from renault_mqtt.support import resolve_support

        async def probe(ep):
            if ep == "soc-levels":
                return True
            raise RuntimeError("still pending")

        await resolve_support(probe, supported, unresolved, lambda ep, err: None)

    calls = {"n": 0}

    async def wait_for(coro, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            await coro
            raise asyncio.TimeoutError
        return await coro

    class OneShot:
        def __init__(self):
            self.n = 0

        def is_set(self):
            self.n += 1
            return self.n > 1

        def set(self):
            pass

        async def wait(self):
            return None

    asyncio.run(run_main(
        addon_name="Test", version="1.0", setup_logging=lambda: None,
        run_command="h", start_health_server=lambda **kw: asyncio.sleep(
            0, types.SimpleNamespace(cleanup=lambda: asyncio.sleep(0))),
        load_state=dict, save_state=lambda s: None, latest=latest,
        vsession_factory=_FakeVS, support_endpoints={"soc-levels", "hvac-settings"},
        initial_supported=initial, detect_supported=detect, run_deploy=lambda **kw: asyncio.sleep(0),
        poll_once=lambda *a: asyncio.sleep(0, ({"battery_level": 50}, None)),
        api_timeout_total=60, log=LOG, event_factory=OneShot, wait_for=wait_for,
    ))
    assert latest["supported"] == ["hvac-settings", "soc-levels"]
    assert initial == {"hvac-settings"}


def test_run_main_preserves_optimistic_defaults_on_startup_probe_errors(monkeypatch):
    _wire_harness(monkeypatch)
    latest = {"version": "1.0", "ok": False, "last_poll": None, "supported": []}

    async def detect(vs, supported=None, unresolved=None):
        from renault_mqtt.support import resolve_support

        async def probe(ep):
            if ep == "actions/charge-start":
                return False
            raise RuntimeError("probe unavailable")

        await resolve_support(probe, supported, unresolved, lambda ep, err: None)

    class OneShot:
        def __init__(self):
            self.n = 0

        def is_set(self):
            self.n += 1
            return self.n > 1

        def set(self):
            pass

        async def wait(self):
            return None

    asyncio.run(run_main(
        addon_name="Test", version="1.0", setup_logging=lambda: None,
        run_command="h", start_health_server=lambda **kw: asyncio.sleep(
            0, types.SimpleNamespace(cleanup=lambda: asyncio.sleep(0))),
        load_state=dict, save_state=lambda s: None, latest=latest,
        vsession_factory=_FakeVS,
        support_endpoints={"actions/charge-start", "charge-mode", "pressure"},
        initial_supported={"actions/charge-start", "charge-mode", "pressure"},
        detect_supported=detect, run_deploy=lambda **kw: asyncio.sleep(0),
        poll_once=lambda *a: asyncio.sleep(0, ({"battery_level": 50}, None)),
        api_timeout_total=60, log=LOG, event_factory=OneShot,
    ))
    assert latest["supported"] == ["charge-mode", "pressure"]


def test_run_main_health_deadline_covers_poll_and_support_retry(monkeypatch):
    fc, _ = _wire_harness(monkeypatch, interval="300")
    latest = {"version": "1.0", "ok": False, "last_poll": None, "supported": []}
    deadlines = []

    async def start_health(health_state=None):
        deadlines.append(health_state)
        return types.SimpleNamespace(cleanup=lambda: asyncio.sleep(0))

    async def detect(vs, supported=None, unresolved=None):
        if not supported:
            return
        unresolved.clear()

    async def poll(vs, state, cap, sup, du):
        return ({"battery_level": 50}, None)

    class OneShot:
        def __init__(self):
            self.n = 0

        def is_set(self):
            self.n += 1
            return self.n > 1

        def set(self):
            pass

        async def wait(self):
            return None

    asyncio.run(run_main(
        addon_name="Test", version="1.0", setup_logging=lambda: None,
        run_command="h", start_health_server=start_health,
        load_state=dict, save_state=lambda s: None, latest=latest,
        vsession_factory=_FakeVS, support_endpoints={"pressure"},
        detect_supported=detect, run_deploy=lambda **kw: asyncio.sleep(0),
        poll_once=poll, api_timeout_total=120, log=LOG,
        monotonic=lambda: 0, event_factory=OneShot,
    ))
    # 290 s poll + 120 s support retry + grace, then the same budget after sleep.
    assert deadlines[0]["deadline"] >= 290 + 120 + HEALTH_GRACE + 300


@pytest.mark.parametrize("failure", ["cancel", "deploy"])
def test_run_main_cleans_up_on_cancellation_and_startup_failure(monkeypatch, failure):
    fc, _ = _wire_harness(monkeypatch)
    events = []

    async def close_client():
        await _FakeSnapshotsClient.close(fc)
        events.append("client")

    fc.close = close_client

    class Resource:
        def __init__(self, name):
            self.name = name

        async def close(self):
            events.append(self.name)

        async def cleanup(self):
            events.append(self.name)

    class VS(_FakeVS):
        async def close(self):
            await super().close()
            events.append("session")

    async def deploy(**kw):
        if failure == "deploy":
            raise RuntimeError("deploy failed")

    async def poll(*args):
        raise asyncio.CancelledError

    async def scenario():
        await run_main(
            addon_name="Test", version="1.0", setup_logging=lambda: None,
            run_command="h", start_health_server=lambda **kw: asyncio.sleep(0, Resource("health")),
            load_state=dict, save_state=lambda s: None, latest={},
            vsession_factory=VS, support_endpoints=set(),
            detect_supported=lambda vs, supported=None, unresolved=None: asyncio.sleep(0),
            run_deploy=deploy, poll_once=poll, api_timeout_total=60, log=LOG,
        )

    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        asyncio.run(scenario())
    assert events == ["session", "health", "client"]
    assert fc.closed is True


@pytest.mark.parametrize("failure", ["load", "factory"])
def test_run_main_cleans_health_when_startup_acquisition_fails(monkeypatch, failure):
    _wire_harness(monkeypatch)
    events = []

    class Health:
        async def cleanup(self):
            events.append("health")

    def load_state():
        if failure == "load":
            raise RuntimeError("load failed")
        return {}

    def factory(locale):
        if failure == "factory":
            raise RuntimeError("factory failed")
        return _FakeVS(locale)

    with pytest.raises(RuntimeError):
        asyncio.run(run_main(
            addon_name="Test", version="1.0", setup_logging=lambda: None,
            run_command="h", start_health_server=lambda **kw: asyncio.sleep(0, Health()),
            load_state=load_state, save_state=lambda s: None, latest={},
            vsession_factory=factory, support_endpoints=set(),
            detect_supported=lambda vs, supported=None, unresolved=None: asyncio.sleep(0),
            run_deploy=lambda **kw: asyncio.sleep(0), poll_once=lambda *a: asyncio.sleep(0),
            api_timeout_total=60, log=LOG,
        ))
    assert events == ["health"]


@pytest.mark.parametrize("failure", ["session", "health", "cancel"])
def test_run_main_attempts_every_cleanup_after_an_earlier_cleanup_fails(monkeypatch, failure):
    fc, _ = _wire_harness(monkeypatch)
    events = []

    class Health:
        async def cleanup(self):
            events.append("health")
            if failure == "health":
                raise RuntimeError("health close failed")

    class VS(_FakeVS):
        async def close(self):
            events.append("session")
            if failure in {"session", "cancel"}:
                raise asyncio.CancelledError if failure == "cancel" else RuntimeError("session close failed")

    async def close_client():
        await _FakeSnapshotsClient.close(fc)
        events.append("client")

    fc.close = close_client

    class Stop:
        def is_set(self):
            return True

        def set(self):
            pass

        async def wait(self):
            return None

    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        asyncio.run(run_main(
            addon_name="Test", version="1.0", setup_logging=lambda: None,
            run_command="h", start_health_server=lambda **kw: asyncio.sleep(0, Health()),
            load_state=dict, save_state=lambda s: None, latest={},
            vsession_factory=VS, support_endpoints=set(),
            detect_supported=lambda vs, supported=None, unresolved=None: asyncio.sleep(0),
            run_deploy=lambda **kw: asyncio.sleep(0), poll_once=lambda *a: asyncio.sleep(0),
            api_timeout_total=60, log=LOG, event_factory=Stop,
        ))
    assert events == ["session", "health", "client"]
    assert fc.closed is True


def test_health_server_cleans_runner_when_site_start_fails():
    fake_web = _make_fake_web()

    class FailingSite:
        def __init__(self, runner, host, port):
            self.runner = runner

        async def start(self):
            raise RuntimeError("bind failed")

    fake_web.TCPSite = FailingSite
    holder = {}

    class Runner(fake_web.AppRunner):
        def __init__(self, app):
            super().__init__(app)
            holder["runner"] = self

    fake_web.AppRunner = Runner
    with pytest.raises(RuntimeError, match="bind failed"):
        asyncio.run(start_health_server(
            panel_file="unused", latest={}, log=LOG, port=0, web_mod=fake_web,
        ))
    assert holder["runner"].cleaned_up is True
