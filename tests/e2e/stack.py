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
NETWORK = f"{PREFIX}-net"
BROKER = f"{PREFIX}-mqtt"
HA = f"{PREFIX}-ha"

MOSQUITTO_CONF = "listener 1883 0.0.0.0\nallow_anonymous true\npersistence false\n"

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
    _docker("rm", "-f", HA, BROKER, check=False)
    _docker("network", "rm", NETWORK, check=False)


def up(ha_version, http_port, mqtt_port):
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
