"""Docker lifecycle for the e2e stack: one network, one Mosquitto broker, one Home Assistant.

Every name carries the ``rmqtt-e2e-`` prefix so a leftover from another session (the r5 session
used ``r5x-*``) can never be mistaken for, or removed as, ours. Config files are copied in with
``docker cp`` rather than bind-mounted, so the stack does not depend on which host paths Docker
Desktop happens to share.
"""
import os
import subprocess
import tempfile
import time

PREFIX = "rmqtt-e2e"
NETWORK = BROKER = HA = None


def name_stack(http_port):
    # Names carry the HA port: up() force-removes its own names first, so a shared name would let
    # a second run on other ports tear down the first mid-scenario.
    global NETWORK, BROKER, HA
    base = f"{PREFIX}-{http_port}"
    NETWORK, BROKER, HA = f"{base}-net", f"{base}-mqtt", f"{base}-ha"


# Persistence on, as the Mosquitto add-on ships it: a broker restart (d2) must keep retained configs.
MOSQUITTO_CONF = ("listener 1883 0.0.0.0\nallow_anonymous true\npersistence true\n"
                  "persistence_location /mosquitto/data/\n")

# mqtt debug is what the harness greps for "Updating component" / "Removing component": the only
# evidence that a discovery message which changed nothing visible was processed at all.
HA_CONF = """default_config:
logger:
  default: warning
  logs:
    homeassistant.components.mqtt: debug
    homeassistant.helpers.entity_registry: debug
"""


def _docker(*args, check=True, capture=True):
    res = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {res.stderr.strip()}")
    return res


def _copy_in(container, content, dest):
    with tempfile.NamedTemporaryFile("w", suffix=os.path.basename(dest), delete=False) as fh:
        fh.write(content)
        src = fh.name
    try:
        _docker("cp", src, f"{container}:{dest}")
    finally:
        os.unlink(src)


def teardown():
    """Remove our containers and network. Safe to call when none exist."""
    if HA is None:
        return
    _docker("rm", "-f", HA, BROKER, check=False)
    _docker("network", "rm", NETWORK, check=False)


def up(ha_version, http_port, mqtt_port):
    name_stack(http_port)
    teardown()
    _docker("network", "create", NETWORK)
    _docker("create", "--name", BROKER, "--network", NETWORK,
            "-p", f"127.0.0.1:{mqtt_port}:1883", "eclipse-mosquitto:2")
    _copy_in(BROKER, MOSQUITTO_CONF, "/mosquitto/config/mosquitto.conf")
    _docker("start", BROKER)
    _docker("create", "--name", HA, "--network", NETWORK, "-e", "TZ=UTC",
            "-p", f"127.0.0.1:{http_port}:8123",
            f"ghcr.io/home-assistant/home-assistant:{ha_version}")
    _copy_in(HA, HA_CONF, "/config/configuration.yaml")
    _docker("start", HA)


def stop_ha():
    # HA flushes its registries on a clean shutdown; the default 10 s can cut that short.
    _docker("stop", "-t", "60", HA)


def start_ha():
    _docker("start", HA)


def stop_broker():
    # SIGTERM is what makes Mosquitto write its persistence file.
    _docker("stop", "-t", "30", BROKER)


def start_broker():
    _docker("start", BROKER)


def ha_file(path):
    """Read a file inside the HA container (read-only; the registries are never written this way)."""
    return _docker("exec", HA, "cat", path).stdout


def ha_logs(since=None):
    args = ["logs", HA]
    if since is not None:
        args[1:1] = ["--since", str(int(since))]
    res = _docker(*args, check=False)
    return res.stdout + res.stderr


def image_digest(ha_version):
    res = _docker("image", "inspect", "--format", "{{index .RepoDigests 0}}",
                  f"ghcr.io/home-assistant/home-assistant:{ha_version}", check=False)
    return res.stdout.strip() or None


def now():
    return time.time()
