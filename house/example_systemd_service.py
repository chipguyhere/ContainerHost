#!/home/pi/venv/bin/python3
"""Example of a house-rules service that runs under systemd instead of Docker.

This file is a template, not a service: copy it to
~/Documents/<service-name>/<service_name>.py, rename SERVICE_NAME and
SERVICE_DESCRIPTION, and replace do_work() with the real job.

The point it demonstrates is that the script carries its own installer. The
systemd unit file lives in /etc/systemd/system, outside the ~/Documents backup
tree, so instead of backing it up the script can always write it again:

    sudo ~/venv/bin/python3 <service_name>.py --installservice
    sudo ~/venv/bin/python3 <service_name>.py --installservice --anyway   # overwrite

The unit runs the script with the same interpreter that ran the installer
(sys.executable), from the script's own path, as the user who ran sudo — so
install with ~/venv's python, from the directory the service will live in.
After installing, `systemctl start <name>` or reboot; `house/services` lists
and manages it because the unit name starts with "mcw".

Everything else follows the house rules for any MQTT script: broker
mqtt.local:8883 over TLS with ca.crt beside the script, QoS 0, a retained
online-status topic with a will, and "0" published on every orderly exit.
Credentials come from a .env file beside the script.
"""

import os
import subprocess
import sys

# Service configuration for systemd installation. The "mcw" prefix is what
# house/services matches on to list the unit.
SERVICE_NAME = "mcw_example"
SERVICE_DESCRIPTION = "mcw Example house-rules service"

# Install as service, if requested. This runs before any third-party import,
# so --installservice works even before the dependencies are in place.
if __name__ == "__main__" and "--installservice" in sys.argv:
    script = os.path.abspath(__file__)
    if os.path.basename(os.path.dirname(script)) == "house":
        print("This is the template in house/. Copy it to its own service directory first.")
        sys.exit(1)
    if os.geteuid() != 0:
        print("Must run as root to install service")
        sys.exit(1)
    sfn = f"/etc/systemd/system/{SERVICE_NAME}.service"
    if "--anyway" not in sys.argv and os.path.exists(sfn):
        print("Service seems to already exist, add --anyway to force overwrite")
        sys.exit(1)
    # SUDO_USER is the user who invoked sudo; os.getlogin() needs a terminal.
    user = os.environ.get("SUDO_USER") or os.getlogin()
    try:
        with open(sfn, "w") as f:
            f.write(f"""[Unit]
Description={SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={sys.executable} -u {script}
Restart=always
RestartSec=5
User={user}
WorkingDirectory={os.path.dirname(script)}

[Install]
WantedBy=multi-user.target
""")
        # daemon-reload so an --anyway overwrite of a loaded unit takes effect.
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True)
        print(f"Installed {sfn}. Reboot or do systemctl start {SERVICE_NAME} to start it.")
    except Exception as e:
        print(f"Failed to install service: {e}")
        sys.exit(1)
    sys.exit(0)

import logging
import pathlib
import signal
import socket
import time

import paho.mqtt.client as mqtt

BASE_DIR = pathlib.Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_env(path=ENV_FILE):
    """Read KEY=VALUE lines from `path` into os.environ; real env vars win."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()

MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER")
MQTT_PASS = os.environ.get("MQTT_PASS")
CA_CERT = BASE_DIR / "ca.crt"

TOPIC_PREFIX = f"example-{socket.gethostname()}"
STATUS_TOPIC = f"{TOPIC_PREFIX}/online-status"
INTERVAL = 30  # seconds between do_work() runs

# Under systemd, stdout goes to the journal: `journalctl -u mcw_example`.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(SERVICE_NAME)

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)


def on_connect(client, userdata, flags, reason_code, properties):
    log.info("Connected to MQTT %s:%s (%s)", MQTT_HOST, MQTT_PORT, reason_code)
    # Re-assert on every connect: a reconnect follows the broker publishing our will.
    client.publish(STATUS_TOPIC, "1", qos=0, retain=True)


def on_disconnect(client, userdata, flags, reason_code, properties):
    log.warning("Disconnected from MQTT %s:%s (%s)", MQTT_HOST, MQTT_PORT, reason_code)


def do_work():
    """The service's actual job. Here: publish the time, retained, as state."""
    client.publish(f"{TOPIC_PREFIX}/last-tick", str(int(time.time())), qos=0, retain=True)


shutting_down = False


def graceful_shutdown():
    global shutting_down
    if shutting_down:
        return
    shutting_down = True
    log.info("Shutting down …")
    client.publish(STATUS_TOPIC, "0", qos=0, retain=True)
    time.sleep(0.5)  # let the publish leave the socket before disconnecting
    client.disconnect()
    client.loop_stop()


def sig_handler(signum, frame):
    graceful_shutdown()
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, sig_handler)  # systemctl stop / restart
    signal.signal(signal.SIGINT, sig_handler)   # Ctrl-C

    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(ca_certs=str(CA_CERT))
    client.will_set(STATUS_TOPIC, "0", qos=0, retain=True)  # before connect()
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    while True:  # the initial connect can fail too; keep trying forever
        try:
            client.connect(MQTT_HOST, MQTT_PORT)
            break
        except Exception:
            log.exception("MQTT connection to %s:%s failed, retrying in 5s", MQTT_HOST, MQTT_PORT)
            time.sleep(5)

    client.loop_start()  # paho reconnects on its own after a drop
    try:
        while True:
            if client.is_connected():
                do_work()
            time.sleep(INTERVAL)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        graceful_shutdown()  # also covers exit via an unhandled exception


if __name__ == "__main__":
    main()
