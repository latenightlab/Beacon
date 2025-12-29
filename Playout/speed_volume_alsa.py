#!/usr/bin/env python3
import time
import subprocess
import paho.mqtt.client as mqtt

# ---- MQTT ----
MQTT_HOST = "192.168.8.10"   # tracker Pi IP
MQTT_PORT = 1883
TOPIC = "sleigh/gps/speed_kph"

# ---- AUDIO ----
MIXER = "Digital"
VOL_STOP = "70%"
VOL_MOVE = "100%"

# ---- LOGIC ----
STOP_BELOW = 1.5
MOVE_ABOVE = 3.0
STOP_FOR = 1.0
MOVE_FOR = 2.0

last_speed = None
state = "UNKNOWN"
below_since = None
above_since = None

def set_volume(percent):
    subprocess.run(
        ["amixer", "sset", MIXER, percent],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

def on_message(client, userdata, msg):
    global last_speed
    try:
        last_speed = float(msg.payload.decode().strip())
    except Exception:
        pass  # ignore anything invalid

client = mqtt.Client(client_id="sleigh-audio")
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.subscribe(TOPIC)
client.loop_start()

while True:
    if last_speed is None:
        time.sleep(0.2)
        continue

    now = time.time()
    s = last_speed

    if s < STOP_BELOW:
        below_since = below_since or now
        above_since = None
    elif s > MOVE_ABOVE:
        above_since = above_since or now
        below_since = None

    if state != "STOPPED" and below_since and (now - below_since) >= STOP_FOR:
        set_volume(VOL_STOP)
        state = "STOPPED"

    elif state != "MOVING" and above_since and (now - above_since) >= MOVE_FOR:
        set_volume(VOL_MOVE)
        state = "MOVING"

    time.sleep(0.25)
