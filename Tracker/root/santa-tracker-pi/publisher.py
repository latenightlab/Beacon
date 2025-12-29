#!/usr/bin/env python3
import time
import json
import math
import requests
import gpsd
import paho.mqtt.client as mqtt

# ---------------- CONFIG ----------------
SERVER_URL = "https://santa.pontypriddroundtable.org.uk/api/update-location"
AUTH_TOKEN = "wefig24qoe9fnqunq08hnwf09dnxqp89r20hf93ndo"
INTERVAL_SECONDS = 1
HTTP_TIMEOUT_SECONDS = 5

# Local MQTT broker (on the Pi)
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883

# Topics your other scripts care about
TOPIC_SPEED_KPH = "sleigh/gps/speed_kph"   # you subscribed to this
TOPIC_STATUS    = "sleigh/gps/status"      # optional JSON status blob
# ----------------------------------------

# WGS84 constants for ECEF -> lat/lon conversion
A = 6378137.0
F = 1 / 298.257223563
E2 = F * (2 - F)

def ecef_to_latlon(x, y, z):
    """Convert ECEF (meters) to lat/lon degrees. Robust for normal GNSS use."""
    b = A * (1 - F)
    ep2 = (A*A - b*b) / (b*b)
    p = math.sqrt(x*x + y*y)
    if p < 1e-6:
        lat = math.copysign(math.pi/2, z)
        lon = 0.0
        return math.degrees(lat), math.degrees(lon)

    lon = math.atan2(y, x)
    theta = math.atan2(z * A, p * b)
    st, ct = math.sin(theta), math.cos(theta)
    lat = math.atan2(z + ep2 * b * st**3, p - E2 * A * ct**3)
    return math.degrees(lat), math.degrees(lon)

def latlon_invalid(lat, lon):
    return lat is None or lon is None or (abs(lat) < 1e-9 and abs(lon) < 1e-9)

def main():
    # MQTT client (async connect so it won't block boot)
    mqtt_client = mqtt.Client(client_id="sleigh-tracker")
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    mqtt_client.loop_start()

    # GPSD connect
    gpsd.connect()

    session = requests.Session()

    while True:
        loop_start = time.time()

        try:
            packet = gpsd.get_current()
            mode = getattr(packet, "mode", 1) or 1

            # Raw values from gpsd
            lat = getattr(packet, "lat", None)
            lon = getattr(packet, "lon", None)
            speed_mps = getattr(packet, "hspeed", 0.0) or 0.0  # m/s
            speed_kph = speed_mps * 3.6

            # If lat/lon are stuck at 0.0 but we have ECEF, convert
            if latlon_invalid(lat, lon):
                x = getattr(packet, "ecefx", None)
                y = getattr(packet, "ecefy", None)
                z = getattr(packet, "ecefz", None)
                if x is not None and y is not None and z is not None:
                    lat, lon = ecef_to_latlon(float(x), float(y), float(z))

            fix_ok = (mode >= 2) and (not latlon_invalid(lat, lon))

            # ---- MQTT: ALWAYS publish speed (so volume logic keeps working) ----
            mqtt_client.publish(TOPIC_SPEED_KPH, f"{speed_kph:.2f}", qos=0, retain=True)

            # Optional richer status topic (handy for debugging)
            status = {
                "timestamp": time.time(),
                "mode": int(mode),
                "fix_ok": bool(fix_ok),
                "lat": float(lat) if lat is not None else None,
                "lon": float(lon) if lon is not None else None,
                "speed_mps": float(speed_mps),
                "speed_kph": float(speed_kph),
            }
            mqtt_client.publish(TOPIC_STATUS, json.dumps(status), qos=0, retain=False)

            # ---- HTTP: only POST if we have a usable fix ----
            if fix_ok:
                payload = {
                    "token": AUTH_TOKEN,
                    "lat": float(lat),
                    "lon": float(lon),
                    "speed": float(speed_mps),       # keep server expecting m/s
                    "timestamp": time.time(),        # time of send
                }
                print(f"Sending: {payload}")

                try:
                    r = session.post(SERVER_URL, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
                    print(f"Server response: {r.status_code} {r.text[:200]}")
                except Exception as e:
                    # Don't kill the loop; 4G/DNS flaps are normal
                    print(f"Error posting to server: {e}")
            else:
                print("No usable GPS fix yet – not posting to server (MQTT still published)")

        except Exception as e:
            # Never exit; keep the service alive
            print(f"Loop error: {e}")

        # Keep roughly stable timing
        elapsed = time.time() - loop_start
        time.sleep(max(1, INTERVAL_SECONDS - elapsed))

if __name__ == "__main__":
    main()
