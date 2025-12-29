#!/usr/bin/env python3
import os
import subprocess
import time
import logging
import shutil
from collections import deque
from threading import Thread, Event

from flask import Flask, jsonify, send_from_directory, request, abort
import paho.mqtt.client as mqtt

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "80"))

MQTT_HOST = os.environ.get("MQTT_HOST", "192.168.8.10")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "sleigh/gps/#")
# MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
# MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")

# Services we want to report on (override with env if your unit names differ)
UNITS = [
    os.environ.get("MOPIDY_UNIT", "mopidy.service"),
    os.environ.get("SUPERVISOR_UNIT", "audio-supervisor.service"),
    os.environ.get("RASPOTIFY_UNIT", "raspotify.service"),
    os.environ.get("SPEEDVOL_UNIT", "speed-volume.service"),
]

MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES", "5"))
messages = deque(maxlen=MAX_MESSAGES)

# Internet check target (default: Google DNS)
PING_TARGET = os.environ.get("PING_TARGET", "8.8.8.8")

app = Flask(__name__, static_folder="static")
stop_evt = Event()

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("werkzeug").setLevel(logging.WARNING)
app.logger.setLevel(logging.WARNING)

mqtt_connected = False
mqtt_last_error = ""
mqtt_last_connect = ""
mqtt_last_subscribe = ""


def run(cmd, timeout=2):
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 999, "", str(e)


def is_authorized():
    """
    If ADMIN_TOKEN is set, require it.
    If not set, only allow from localhost for safety.
    """
    if ADMIN_TOKEN:
        tok = request.headers.get("X-Auth-Token", "")
        return tok == ADMIN_TOKEN
    return request.remote_addr in ("127.0.0.1", "::1")


def internet_ping_status():
    """
    Ping 8.8.8.8 (or PING_TARGET) to decide if we have internet.
    Returns: {ok: bool, target: str, latency_ms: float|None, detail: str}
    """
    target = PING_TARGET

    # Prefer system ping if available
    if shutil.which("ping"):
        # -c 1 = 1 packet, -W 1 = 1s timeout (Linux iputils)
        rc, out, err = run(["ping", "-c", "1", "-W", "1", target], timeout=2)

        if rc == 0 and out:
            latency_ms = None

            # Parse "time=12.3 ms"
            import re
            m = re.search(r"time[=<]([\d\.]+)\s*ms", out)
            if m:
                try:
                    latency_ms = float(m.group(1))
                except Exception:
                    latency_ms = None

            return {
                "ok": True,
                "target": target,
                "latency_ms": latency_ms,
                "detail": "ping ok",
            }

        return {
            "ok": False,
            "target": target,
            "latency_ms": None,
            "detail": (err or out or "ping failed")[:200],
        }

    # Fallback (no ping binary): attempt a TCP connect to DNS port
    import socket
    try:
        s = socket.create_connection((target, 53), timeout=1.0)
        s.close()
        return {"ok": True, "target": target, "latency_ms": None, "detail": "tcp/53 ok (no ping cmd)"}
    except Exception as e:
        return {"ok": False, "target": target, "latency_ms": None, "detail": f"no ping cmd + tcp failed: {e}"}


def systemctl_action(unit, action):
    if action not in ("start", "stop", "restart"):
        return 400, "", f"Invalid action: {action}"
    return run(["systemctl", action, unit], timeout=10)


@app.route("/api/service/<path:unit>/<action>", methods=["POST"])
def api_service_action(unit, action):
    if not is_authorized():
        abort(403)

    # SAFETY: only allow controlling units we expose in /api/status
    allowed_units = set(UNITS)
    if unit not in allowed_units:
        return jsonify({
            "ok": False,
            "unit": unit,
            "action": action,
            "rc": 403,
            "stdout": "",
            "stderr": "Unit not permitted by this UI"
        }), 403

    rc, out, err = systemctl_action(unit, action)
    ok = (rc == 0)
    return jsonify({
        "ok": ok,
        "unit": unit,
        "action": action,
        "rc": rc,
        "stdout": out,
        "stderr": err,
    }), (200 if ok else 500)


@app.route("/api/power/<action>", methods=["POST"])
def api_power(action):
    """
    action: reboot | shutdown
    """
    if not is_authorized():
        abort(403)

    if action not in ("reboot", "shutdown"):
        return jsonify({"ok": False, "stderr": "Invalid action"}), 400

    cmd = ["systemctl", "reboot"] if action == "reboot" else ["systemctl", "poweroff"]

    # Return quickly; systemctl will start the transition and may kill us.
    rc, out, err = run(cmd, timeout=2)
    ok = (rc == 0)

    return jsonify({
        "ok": ok,
        "action": action,
        "rc": rc,
        "stdout": out,
        "stderr": err,
    }), (200 if ok else 500)


def systemd_is_active(unit):
    rc, out, err = run(["systemctl", "is-active", unit])
    if rc == 0:
        return True, out
    return False, out or err


# def systemd_status_tail(unit):
#     rc, out, err = run(["systemctl", "status", unit, "--no-pager", "-n", "5"])
#     text = out or err
#     lines = [ln for ln in text.splitlines() if ln.strip()]
#     return "\n".join(lines[-2:]) if lines else ""

def systemd_status_tail(unit, lines_to_fetch=10, tail_lines=2):
    """
    Return the last `tail_lines` non-empty lines from `systemctl status`.
    - lines_to_fetch: how many lines systemctl should include
    - tail_lines: how many of those we return to the UI
    """
    rc, out, err = run(["systemctl", "status", unit, "--no-pager", "-n", str(lines_to_fetch)])
    text = out or err
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-tail_lines:]) if lines else ""


def mqtt_on_connect(client, userdata, flags, rc):
    global mqtt_connected, mqtt_last_connect, mqtt_last_subscribe
    mqtt_last_connect = f"rc={rc}"
    if rc == 0:
        mqtt_connected = True
        (result, mid) = client.subscribe(MQTT_TOPIC)
        mqtt_last_subscribe = f"subscribe result={result} mid={mid} topic={MQTT_TOPIC}"
    else:
        mqtt_connected = False


def mqtt_on_message(client, userdata, msg):
    logging.debug("MQTT msg topic=%s", msg.topic)

    try:
        payload = msg.payload.decode("utf-8", errors="replace")
    except Exception:
        payload = repr(msg.payload)

    messages.appendleft({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "topic": msg.topic,
        "payload": payload[:500],
        "qos": msg.qos,
        "retain": msg.retain,
    })


def mqtt_thread():
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)

    # If you later want auth, uncomment these AND re-add MQTT_USERNAME/PASSWORD above.
    # if MQTT_USERNAME:
    #     client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_connect = mqtt_on_connect
    client.on_message = mqtt_on_message

    while not stop_evt.is_set():
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            client.loop_start()
            while not stop_evt.is_set():
                time.sleep(0.2)
            client.loop_stop()
            client.disconnect()
        except Exception as e:
            global mqtt_connected, mqtt_last_error
            mqtt_connected = False
            mqtt_last_error = str(e)
            time.sleep(1.0)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def api_status():
    services = {}
    for unit in UNITS:
        ok, state = systemd_is_active(unit)
        services[unit] = {
            "unit": unit,
            "active": ok,
            "state": state,
            "tail": systemd_status_tail(unit),
        }

    net = internet_ping_status()

    return jsonify({
        "services": services,
        "internet": net,
        "mqtt": {
            "connected": mqtt_connected,
            "last_error": mqtt_last_error,
            "last_connect": mqtt_last_connect,
            "last_subscribe": mqtt_last_subscribe,
            "subscribed_topic": MQTT_TOPIC,
            "last_messages": list(messages),
        },
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    })

# return jsonify({
#     "services": {
#         "gpsd": {"unit": GPSD_UNIT, "active": gpsd_ok, "state": gpsd_state, "tail": systemd_status_tail(GPSD_UNIT, 10, 2)},
#         "mqtt_broker": {"unit": MQTT_BROKER_UNIT, "active": broker_ok, "state": broker_state, "tail": systemd_status_tail(MQTT_BROKER_UNIT, 10, 2)},
#         "publisher": {"unit": PUBLISHER_UNIT, "active": pub_ok, "state": pub_state, "tail": systemd_status_tail(PUBLISHER_UNIT, 10, 2)},
#     },
#     ...
# })


if __name__ == "__main__":
    Thread(target=mqtt_thread, daemon=True).start()
    app.run(host=APP_HOST, port=APP_PORT)
