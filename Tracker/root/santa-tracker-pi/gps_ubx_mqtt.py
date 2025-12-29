#!/usr/bin/env python3
"""
gps_ubx_mqtt.py
Direct u-blox (VK-162 style) UBX NAV-PVT reader -> MQTT publisher.
Replaces gpsd entirely.

Publishes:
  <BASE>/position   (JSON, retained)  {timestamp, lat, lon, speed_mps, speed_kmh, fix_ok, numSV}
  <BASE>/lat        (float)
  <BASE>/lon        (float)
  <BASE>/speed_mps  (float)
  <BASE>/speed_kmh  (float)
  <BASE>/fix_ok     (0/1)
  <BASE>/numsat     (int)

Default BASE = sleigh/gps
"""

import os
import time
import json
import struct
import logging
from typing import Optional, Dict, Tuple

import serial  # pyserial
import paho.mqtt.client as mqtt


# -----------------------
# Config (env overrides)
# -----------------------
SERIAL_DEV = os.environ.get("GPS_DEV", "/dev/ttyACM0")
SERIAL_BAUD = int(os.environ.get("GPS_BAUD", "9600"))
SERIAL_TIMEOUT = float(os.environ.get("GPS_TIMEOUT", "1.0"))

MQTT_HOST = os.environ.get("MQTT_HOST", "192.168.1.211")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "sleigh-gps-ubx")
MQTT_TOPIC_BASE = os.environ.get("MQTT_TOPIC_BASE", "sleigh/gps")

PUBLISH_RETAIN = os.environ.get("MQTT_RETAIN", "true").lower() in ("1", "true", "yes", "y")
PUBLISH_QOS = int(os.environ.get("MQTT_QOS", "0"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


# -----------------------
# UBX helpers
# -----------------------
SYNC1 = 0xB5
SYNC2 = 0x62

CLASS_NAV = 0x01
ID_NAV_PVT = 0x07  # NAV-PVT (length 92)

CLASS_CFG = 0x06
ID_CFG_MSG = 0x01  # CFG-MSG


def ubx_checksum(data: bytes) -> Tuple[int, int]:
    """UBX checksum over: class, id, length(2), payload"""
    ck_a = 0
    ck_b = 0
    for b in data:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def ubx_packet(msg_class: int, msg_id: int, payload: bytes = b"") -> bytes:
    length = len(payload)
    hdr = bytes([msg_class, msg_id]) + struct.pack("<H", length) + payload
    ck_a, ck_b = ubx_checksum(hdr)
    return bytes([SYNC1, SYNC2]) + hdr + bytes([ck_a, ck_b])


def cfg_msg_payload_ubx6(msg_class: int, msg_id: int,
                        rate_i2c: int, rate_uart1: int, rate_uart2: int, rate_usb: int) -> bytes:
    """
    UBX-6 style CFG-MSG payload: msgClass, msgID, rateI2C, rateUART1, rateUART2, rateUSB
    (Many VK-162/u-blox 6 devices accept this 6-byte form.)
    """
    return bytes([msg_class, msg_id, rate_i2c & 0xFF, rate_uart1 & 0xFF, rate_uart2 & 0xFF, rate_usb & 0xFF])


def parse_nav_pvt(payload: bytes) -> Dict[str, object]:
    """
    Parse UBX-NAV-PVT (92 bytes).
    Returns dict with lat/lon (deg), speed (m/s), fix flags, satellites, timestamp.
    """
    if len(payload) != 92:
        raise ValueError(f"NAV-PVT payload len {len(payload)} != 92")

    # Offsets per u-blox NAV-PVT spec (little-endian)
    iTOW = struct.unpack_from("<I", payload, 0)[0]
    year = struct.unpack_from("<H", payload, 4)[0]
    month = payload[6]
    day = payload[7]
    hour = payload[8]
    minute = payload[9]
    sec = payload[10]
    valid = payload[11]
    fixType = payload[20]
    flags = payload[21]
    numSV = payload[23]
    lon = struct.unpack_from("<i", payload, 24)[0] / 1e7
    lat = struct.unpack_from("<i", payload, 28)[0] / 1e7
    gSpeed_mps = struct.unpack_from("<i", payload, 60)[0] / 1000.0  # mm/s -> m/s

    # flags bit 0 = gnssFixOK
    fix_ok = bool(flags & 0x01)

    # Valid time flags: bit0 validDate, bit1 validTime
    time_ok = bool(valid & 0x03)

    # ISO-ish timestamp (device time). Also publish epoch time locally.
    ts_str = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{sec:02d}Z"

    return {
        "iTOW_ms": iTOW,
        "time": ts_str,
        "time_ok": time_ok,
        "fix_type": int(fixType),
        "fix_ok": fix_ok,
        "numSV": int(numSV),
        "lat": float(lat),
        "lon": float(lon),
        "speed_mps": float(gSpeed_mps),
        "speed_kmh": float(gSpeed_mps * 3.6),
        "timestamp": float(time.time()),
    }


class UbxReader:
    def __init__(self, dev: str, baud: int, timeout: float):
        self.ser = serial.Serial(dev, baudrate=baud, timeout=timeout)
        self.buf = bytearray()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def write_ubx(self, msg_class: int, msg_id: int, payload: bytes = b""):
        pkt = ubx_packet(msg_class, msg_id, payload)
        self.ser.write(pkt)
        self.ser.flush()

    def configure_receiver(self):
        """
        Make the receiver stream NAV-PVT on USB, and disable the stale NMEA sentences.
        Uses the UBX-6 style CFG-MSG form that matches your working ubxtool commands.
        """
        # Enable NAV-PVT on USB: rates (I2C=0, UART1=0, UART2=0, USB=1)
        self.write_ubx(CLASS_CFG, ID_CFG_MSG, cfg_msg_payload_ubx6(CLASS_NAV, ID_NAV_PVT, 0, 0, 0, 1))

        # Disable NMEA on USB (class 0xF0):
        # GGA = F0 00, RMC = F0 04, ZDA = F0 08
        self.write_ubx(CLASS_CFG, ID_CFG_MSG, cfg_msg_payload_ubx6(0xF0, 0x00, 0, 0, 0, 0))
        self.write_ubx(CLASS_CFG, ID_CFG_MSG, cfg_msg_payload_ubx6(0xF0, 0x04, 0, 0, 0, 0))
        self.write_ubx(CLASS_CFG, ID_CFG_MSG, cfg_msg_payload_ubx6(0xF0, 0x08, 0, 0, 0, 0))

    def read_packet(self) -> Optional[Tuple[int, int, bytes]]:
        """
        Read and return next UBX packet (class,id,payload). Returns None if not enough data yet.
        """
        chunk = self.ser.read(4096)
        if chunk:
            self.buf.extend(chunk)

        # Find sync
        while True:
            if len(self.buf) < 8:
                return None

            # Find 0xB5 0x62
            sync_idx = self.buf.find(bytes([SYNC1, SYNC2]))
            if sync_idx == -1:
                # Drop all
                self.buf.clear()
                return None
            if sync_idx > 0:
                del self.buf[:sync_idx]

            if len(self.buf) < 8:
                return None

            msg_class = self.buf[2]
            msg_id = self.buf[3]
            length = struct.unpack_from("<H", self.buf, 4)[0]
            total_len = 2 + 4 + length + 2  # sync(2) + class/id/len(4) + payload + ck(2)
            if len(self.buf) < total_len:
                return None

            pkt = bytes(self.buf[:total_len])
            del self.buf[:total_len]

            # Validate checksum
            hdr_plus_payload = pkt[2:-2]  # class..payload
            ck_a, ck_b = ubx_checksum(hdr_plus_payload)
            if ck_a != pkt[-2] or ck_b != pkt[-1]:
                # Bad packet; continue searching
                continue

            payload = pkt[6:-2]
            return msg_class, msg_id, payload


def mqtt_connect() -> mqtt.Client:
    c = mqtt.Client(client_id=MQTT_CLIENT_ID, clean_session=True)
    if MQTT_USERNAME:
        c.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    def on_connect(client, userdata, flags, rc, properties=None):
        logging.info("MQTT connected rc=%s", rc)
        client.publish(f"{MQTT_TOPIC_BASE}/status", "online", qos=1, retain=True)

    def on_disconnect(client, userdata, rc, properties=None):
        logging.warning("MQTT disconnected rc=%s", rc)

    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.will_set(f"{MQTT_TOPIC_BASE}/status", "offline", qos=1, retain=True)

    c.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    c.loop_start()
    return c


def publish_fix(mq: mqtt.Client, fix: Dict[str, object]):
    base = MQTT_TOPIC_BASE
    mq.publish(f"{base}/position", json.dumps(fix, separators=(",", ":")), qos=PUBLISH_QOS, retain=PUBLISH_RETAIN)
    mq.publish(f"{base}/lat", str(fix["lat"]), qos=PUBLISH_QOS, retain=False)
    mq.publish(f"{base}/lon", str(fix["lon"]), qos=PUBLISH_QOS, retain=False)
    mq.publish(f"{base}/speed_mps", str(fix["speed_mps"]), qos=PUBLISH_QOS, retain=False)
    mq.publish(f"{base}/speed_kmh", str(fix["speed_kmh"]), qos=PUBLISH_QOS, retain=False)
    mq.publish(f"{base}/fix_ok", "1" if fix["fix_ok"] else "0", qos=PUBLISH_QOS, retain=False)
    mq.publish(f"{base}/numsat", str(fix["numSV"]), qos=PUBLISH_QOS, retain=False)


def main():
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    mq = mqtt_connect()
    reader = UbxReader(SERIAL_DEV, SERIAL_BAUD, SERIAL_TIMEOUT)

    try:
        logging.info("Opening GPS on %s @ %d", SERIAL_DEV, SERIAL_BAUD)

        # Configure once on startup (safe even if already set)
        reader.configure_receiver()
        logging.info("Configured receiver: NAV-PVT on USB, NMEA disabled on USB")

        last_pub = 0.0
        last_fix: Optional[Dict[str, object]] = None

        while True:
            pkt = reader.read_packet()
            if not pkt:
                continue

            msg_class, msg_id, payload = pkt
            if msg_class == CLASS_NAV and msg_id == ID_NAV_PVT:
                fix = parse_nav_pvt(payload)
                last_fix = fix

                # publish at most 1 Hz by default (your receiver iTOW changes each second anyway)
                now = time.time()
                if now - last_pub >= 1.0:
                    publish_fix(mq, fix)
                    last_pub = now

            # Optional: publish "no fix" heartbeat every 5s if we aren't seeing NAV-PVT
            if last_fix and (time.time() - last_fix["timestamp"] > 5.0):
                mq.publish(f"{MQTT_TOPIC_BASE}/fix_ok", "0", qos=PUBLISH_QOS, retain=False)

    except KeyboardInterrupt:
        logging.info("Exiting on Ctrl+C")
    finally:
        try:
            mq.publish(f"{MQTT_TOPIC_BASE}/status", "offline", qos=1, retain=True)
        except Exception:
            pass
        try:
            mq.loop_stop()
        except Exception:
            pass
        reader.close()


if __name__ == "__main__":
    main()
