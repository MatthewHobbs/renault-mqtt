"""Home Assistant over its public APIs only: HTTP for onboarding and config flows, the websocket
API for the registries. Nothing here reaches into HA's process, because an in-process
``entity_registry.async_get_or_create`` shortcut was tried and is unfaithful: it drops the entity
name, which is exactly what entity-id generation depends on.

The websocket client is a minimal RFC 6455 implementation so the harness needs nothing beyond the
standard library and paho-mqtt.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class HAError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, host, port, path):
        self._sock = socket.create_connection((host, port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        self._rfile = self._sock.makefile("rb")
        status = self._rfile.readline().decode()
        headers = {}
        while (line := self._rfile.readline().decode().strip()):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        if " 101 " not in status:
            raise HAError(f"websocket upgrade refused: {status.strip()}")
        expect = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expect:
            raise HAError("websocket accept key mismatch")

    def _send_frame(self, opcode, data):
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        self._sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read_exact(self, n):
        buf = self._rfile.read(n)
        if buf is None or len(buf) != n:
            raise HAError("websocket closed mid-frame")
        return buf

    def send(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode())

    def recv(self):
        parts = []
        while True:
            b0, b1 = self._read_exact(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            payload = self._read_exact(n)          # server frames are never masked
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise HAError("websocket closed by server")
            if opcode in (0x1, 0x0):
                parts.append(payload)
                if b0 & 0x80:
                    return json.loads(b"".join(parts))

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self._sock.close()


class HA:
    def __init__(self, port):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.client_id = f"{self.base}/"
        self.token = None
        self._ws = None
        self._msg_id = 0

    # ------------------------------------------------------------------ HTTP
    def _http(self, method, path, body=None, form=False, auth=True, timeout=30):
        headers = {}
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            raise HAError(f"{method} {path} -> {err.code}: {err.read().decode(errors='replace')}") from err
        return json.loads(raw) if raw else None

    def wait_http(self, timeout=300):
        """Wait for GET /api/onboarding to answer: the earliest endpoint a fresh HA serves."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                return self._http("GET", "/api/onboarding", auth=False, timeout=5)
            except (OSError, HAError) as err:
                last = err
                time.sleep(2)
        raise HAError(f"HA did not answer /api/onboarding within {timeout}s: {last}")

    def wait_api(self, timeout=300):
        """After a restart: wait until the authenticated API answers again."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                return self._http("GET", "/api/config", timeout=5)
            except (OSError, HAError) as err:
                last = err
                time.sleep(2)
        raise HAError(f"HA API did not come back within {timeout}s: {last}")

    def onboard(self):
        user = self._http("POST", "/api/onboarding/users", {
            "client_id": self.client_id, "name": "E2E", "username": "e2e",
            "password": "e2e-harness-password", "language": "en"}, auth=False)
        tok = self._http("POST", "/auth/token", {
            "grant_type": "authorization_code", "code": user["auth_code"],
            "client_id": self.client_id}, form=True, auth=False)
        self.token = tok["access_token"]
        self._http("POST", "/api/onboarding/core_config", {})
        self._http("POST", "/api/onboarding/analytics", {})
        # Without this step the MQTT config flow answers with no flow_id.
        self._http("POST", "/api/onboarding/integration",
                   {"client_id": self.client_id, "redirect_uri": self.client_id})
        # The onboarding access token lasts 30 minutes, and a full run is longer.
        self.token = self.ws_call({"type": "auth/long_lived_access_token",
                                   "client_name": "rmqtt-e2e", "lifespan": 30})
        self.ws_close()

    def add_mqtt(self, broker_host):
        """Run the MQTT config flow. VERSION TRAP: 2026.9.x sections the broker form and requires
        ``other_settings``; 2026.7.1 rejects that key. Send it only when the schema asks for it."""
        step = self._http("POST", "/api/config/config_entries/flow",
                          {"handler": "mqtt", "show_advanced_options": False})
        trail = []
        for _ in range(6):
            trail.append({k: step.get(k) for k in ("type", "step_id", "errors", "reason")})
            if step.get("type") == "create_entry":
                return {"entry_id": step["result"]["entry_id"], "trail": trail}
            if step.get("type") != "form" or "flow_id" not in step:
                raise HAError(f"MQTT flow did not give a form: {step}")
            names = {f.get("name") for f in step.get("data_schema", [])}
            data = {"broker": broker_host, "port": 1883}
            if "other_settings" in names:
                data["other_settings"] = {"set_client_cert": False, "set_ca_cert": "off"}
            trail[-1]["sent_other_settings"] = "other_settings" in names
            step = self._http("POST", f"/api/config/config_entries/flow/{step['flow_id']}", data)
        raise HAError(f"MQTT flow never reached create_entry: {trail}")

    def mqtt_entry_state(self):
        entries = self._http("GET", "/api/config/config_entries/entry?domain=mqtt")
        return entries[0]["state"] if entries else None

    def wait_mqtt_loaded(self, timeout=120):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            try:
                state = self.mqtt_entry_state()
            except (OSError, HAError):
                state = None
            if state == "loaded":
                return
            time.sleep(1)
        raise HAError(f"MQTT config entry not loaded within {timeout}s (last state {state})")

    # ------------------------------------------------------------- websocket
    def _ws_connect(self):
        ws = WebSocket("127.0.0.1", self.port, "/api/websocket")
        hello = ws.recv()
        if hello.get("type") != "auth_required":
            raise HAError(f"unexpected websocket greeting: {hello}")
        ws.send({"type": "auth", "access_token": self.token})
        ok = ws.recv()
        if ok.get("type") != "auth_ok":
            raise HAError(f"websocket auth failed: {ok}")
        self._ws = ws
        self.ha_version = ok.get("ha_version")

    def ws_close(self):
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def ws_call(self, msg):
        for attempt in (0, 1):
            try:
                if self._ws is None:
                    self._ws_connect()
                self._msg_id += 1
                self._ws.send({**msg, "id": self._msg_id})
                while True:
                    resp = self._ws.recv()
                    if resp.get("id") == self._msg_id and resp.get("type") == "result":
                        break
            except (OSError, HAError):
                # A restarted HA drops the socket; reconnect once, then let it raise.
                self.ws_close()
                if attempt:
                    raise
                continue
            if not resp.get("success"):
                raise HAError(f"{msg['type']} failed: {resp.get('error')}")
            return resp.get("result")

    def get_config(self):
        return self.ws_call({"type": "get_config"})

    def entities(self):
        return self.ws_call({"type": "config/entity_registry/list"})

    def entities_by_uid(self):
        return {e["unique_id"]: e for e in self.entities() if e.get("platform") == "mqtt"}

    def devices(self):
        return self.ws_call({"type": "config/device_registry/list"})

    def device_by_identifier(self, ident):
        for d in self.devices():
            if ["mqtt", ident] in d.get("identifiers", []):
                return d
        return None

    def ensure_area(self, name):
        for a in self.ws_call({"type": "config/area_registry/list"}):
            if a["name"] == name:
                return a["area_id"]
        return self.ws_call({"type": "config/area_registry/create", "name": name})["area_id"]

    def update_device(self, device_id, **changes):
        return self.ws_call({"type": "config/device_registry/update", "device_id": device_id, **changes})

    def wait_registry(self, present=(), absent=(), timeout=60, interval=0.5):
        """Poll until every uid in `present` is registered and every uid in `absent` is not.
        Returns (ok, {uid: entry}) where the map is the last snapshot, so a timeout still reports
        what the registry actually held."""
        deadline = time.monotonic() + timeout
        while True:
            snap = self.entities_by_uid()
            ok = all(u in snap for u in present) and not any(u in snap for u in absent)
            if ok or time.monotonic() >= deadline:
                return ok, snap
            time.sleep(interval)
