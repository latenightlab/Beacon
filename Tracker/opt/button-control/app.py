#!/usr/bin/env python3
import glob
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import serial
from flask import Flask, jsonify, request, send_from_directory

# Optional Socket.IO
SOCKETIO_OK = True
try:
    from flask_socketio import SocketIO, emit
except Exception:
    SOCKETIO_OK = False
    SocketIO = None
    emit = None

# -----------------------------
# Config
# -----------------------------
HTTP_BIND = "0.0.0.0"
HTTP_PORT = 8080

# If empty, auto-discover /dev/ttyACM*
SERIAL_PORTS: List[str] = []

SERIAL_BAUD = 115200
SERIAL_TIMEOUT = 0.1
PICO_RECONNECT_INTERVAL_S = 2.0
PICO_PING_INTERVAL_S = 1.0

# I2C raw commands (your relay/controller device)
RAW_I2C_BUS = 1
RAW_I2C_ADDR = 0x10

# Button 0 commands (as requested)
BTN0_REG = 0x01
BTN0_ON  = 0xFF
BTN0_OFF = 0x00

# Button 1 commands (you can change REG if needed)
BTN1_REG = 0x02
BTN1_ON  = 0xFF
BTN1_OFF = 0x00

# Remote audio host for Button 2
AUDIO_HOST = "192.168.196.11"
AUDIO_USER = "ctrlaudio"
AUDIO_CONTROL = "Digital"

# systemd service to control
PUBLISHER_SERVICE = "santa-publisher.service"

# -----------------------------
# Helpers
# -----------------------------
def run_cmd(cmd: List[str]) -> bool:
    try:
        subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return True
    except Exception:
        return False

def i2cset_cmd(bus: int, addr: int, reg: int, value: int) -> None:
    # i2cset -y 1 0x10 0x01 0xFF
    run_cmd([
        "i2cset",
        "-y",
        str(bus),
        hex(addr),
        hex(reg),
        hex(value),
    ])

def ssh_amixer_set(host: str, user: str, control: str, percent: int) -> None:
    # Fail-fast, non-interactive SSH
    run_cmd([
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=2",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        "sudo", "amixer", "sset", control, f"{int(percent)}%"
    ])

def systemctl_restart(service: str) -> None:
    run_cmd(["systemctl", "restart", service])

def systemctl_stop(service: str) -> None:
    run_cmd(["systemctl", "stop", service])

def systemctl_is_active(service: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", service],
            check=False,
            capture_output=True,
            text=True
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False

# -----------------------------
# State
# -----------------------------
@dataclass
class LedState:
    mode: str = "OFF"  # OFF | SOLID | FLASH
    rgb: Tuple[int, int, int] = (0, 0, 0)

@dataclass
class SystemState:
    leds: Dict[int, LedState] = field(default_factory=lambda: {i: LedState() for i in range(4)})

    relay0: bool = False
    relay1: bool = False
    audio_active: bool = False
    publisher_active: bool = False

    def to_dict(self):
        return {
            "leds": {
                str(i): {"mode": self.leds[i].mode, "rgb": list(self.leds[i].rgb)}
                for i in self.leds
            },
            "relay0": self.relay0,
            "relay1": self.relay1,
            "audio_active": self.audio_active,
            "publisher_active": self.publisher_active,
        }

# -----------------------------
# Pico serial client
# -----------------------------
class PicoClient(threading.Thread):
    def __init__(self, port: str, event_q: queue.Queue):
        super().__init__(daemon=True)
        self.port = port
        self.event_q = event_q
        self.ser: Optional[serial.Serial] = None
        self.connected = False
        self.last_rx = 0.0
        self._tx_lock = threading.Lock()

    def info(self) -> dict:
        now = time.time()
        age = None
        if self.last_rx > 0:
            age = round(now - self.last_rx, 2)
        return {"port": self.port, "connected": bool(self.connected), "last_rx_age_s": age}

    def send(self, line: str):
        with self._tx_lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.write((line.strip() + "\n").encode("utf-8"))
                    self.ser.flush()
                except Exception:
                    pass

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, SERIAL_BAUD, timeout=SERIAL_TIMEOUT)
            self.connected = True
            self.last_rx = time.time()
            print(f"[pico] Connected {self.port}")
            self.send("HELLO")
        except Exception as e:
            print(f"[pico] Failed {self.port}: {e}")
            self.connected = False

    def run(self):
        while True:
            if not self.connected:
                self.connect()
                time.sleep(PICO_RECONNECT_INTERVAL_S)
                continue

            try:
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                if line:
                    self.last_rx = time.time()
                    self.event_q.put({"type": "pico_line", "port": self.port, "line": line})
            except Exception:
                self.connected = False

            if time.time() - self.last_rx > PICO_PING_INTERVAL_S:
                self.send("PING")

# -----------------------------
# Hub
# -----------------------------
class Hub:
    def __init__(self, socketio_obj=None):
        self.socketio = socketio_obj
        self.state = SystemState()
        self.state_lock = threading.Lock()
        self.event_q: queue.Queue = queue.Queue()
        self.picos: Dict[str, PicoClient] = {}

        # initial service state
        self.state.publisher_active = systemctl_is_active(PUBLISHER_SERVICE)

    def discover_ports(self) -> List[str]:
        if SERIAL_PORTS:
            return list(SERIAL_PORTS)
        ports = sorted(glob.glob("/dev/ttyACM*")) + sorted(glob.glob("/dev/pico-panel-*"))
        seen, out = set(), []
        for p in ports:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def ensure_picos(self):
        for port in self.discover_ports():
            if port not in self.picos:
                pc = PicoClient(port, self.event_q)
                self.picos[port] = pc
                pc.start()

    def pico_summary(self) -> dict:
        ports = self.discover_ports()
        total = len(ports)
        connected = 0
        details = []

        for p in ports:
            c = self.picos.get(p)
            if c is None:
                details.append({"port": p, "connected": False, "last_rx_age_s": None})
            else:
                d = c.info()
                details.append(d)
                if d["connected"]:
                    connected += 1

        return {"total": total, "connected": connected, "details": details}

    # ----- broadcast -----
    def broadcast_state_to_picos(self):
        for i, led in self.state.leds.items():
            if led.mode == "OFF":
                cmd = f"OFF {i}"
            elif led.mode == "SOLID":
                r, g, b = led.rgb
                cmd = f"RGB {i} {r} {g} {b}"
            else:
                r, g, b = led.rgb
                cmd = f"FLASH {i} {r} {g} {b}"

            for p in self.picos.values():
                if p.connected:
                    p.send(cmd)

    def broadcast_state_to_web(self):
        if self.socketio is not None:
            self.socketio.emit("state", self.state.to_dict())
            self.socketio.emit("picos", self.pico_summary())

    def broadcast_state(self):
        self.broadcast_state_to_picos()
        self.broadcast_state_to_web()

    # ----- actions -----
    def handle_button_event(self, source: str, btn: int, kind: str):
        kind = kind.upper()
        print(f"[evt] {source} btn={btn} {kind}")

        # -------------------------
        # Button 0: raw I2C
        # -------------------------
        if btn == 0:
            if kind == "SINGLE":
                i2cset_cmd(RAW_I2C_BUS, RAW_I2C_ADDR, BTN0_REG, BTN0_ON)
                with self.state_lock:
                    self.state.relay0 = True
                    self.state.leds[0].mode = "SOLID"
                    self.state.leds[0].rgb = (0, 255, 0)

            elif kind == "LONG":
                i2cset_cmd(RAW_I2C_BUS, RAW_I2C_ADDR, BTN0_REG, BTN0_OFF)
                with self.state_lock:
                    self.state.relay0 = False
                    self.state.leds[0].mode = "OFF"
                    self.state.leds[0].rgb = (0, 0, 0)

        # -------------------------
        # Button 1: raw I2C (same style)
        # -------------------------
        elif btn == 1:
            if kind == "SINGLE":
                i2cset_cmd(RAW_I2C_BUS, RAW_I2C_ADDR, BTN1_REG, BTN1_ON)
                with self.state_lock:
                    self.state.relay1 = True
                    self.state.leds[1].mode = "SOLID"
                    self.state.leds[1].rgb = (0, 255, 0)

            elif kind == "LONG":
                i2cset_cmd(RAW_I2C_BUS, RAW_I2C_ADDR, BTN1_REG, BTN1_OFF)
                with self.state_lock:
                    self.state.relay1 = False
                    self.state.leds[1].mode = "OFF"
                    self.state.leds[1].rgb = (0, 0, 0)

        # -------------------------
        # Button 2: remote amixer (latched flash until LONG)
        # SINGLE: set 60% + start flashing
        # LONG:   set 100% + stop flashing
        # -------------------------
        elif btn == 2:
            if kind == "SINGLE":
                ssh_amixer_set(AUDIO_HOST, AUDIO_USER, AUDIO_CONTROL, 60)
                with self.state_lock:
                    self.state.audio_active = True
                    self.state.leds[2].mode = "FLASH"
                    self.state.leds[2].rgb = (0, 120, 255)

            elif kind == "LONG":
                ssh_amixer_set(AUDIO_HOST, AUDIO_USER, AUDIO_CONTROL, 100)
                with self.state_lock:
                    self.state.audio_active = False
                    self.state.leds[2].mode = "OFF"
                    self.state.leds[2].rgb = (0, 0, 0)

        # -------------------------
        # Button 3: service control (latched flash until LONG)
        # SINGLE: restart service + start flashing
        # LONG:   stop service + stop flashing
        # -------------------------
        elif btn == 3:
            if kind == "SINGLE":
                systemctl_restart(PUBLISHER_SERVICE)
                with self.state_lock:
                    self.state.publisher_active = True
                    self.state.leds[3].mode = "FLASH"
                    self.state.leds[3].rgb = (255, 255, 255)

            elif kind == "LONG":
                systemctl_stop(PUBLISHER_SERVICE)
                with self.state_lock:
                    self.state.publisher_active = False
                    self.state.leds[3].mode = "OFF"
                    self.state.leds[3].rgb = (0, 0, 0)

        # IMPORTANT:
        # Do NOT “sync truth” here by calling systemctl_is_active() etc,
        # because that will overwrite your “latched FLASH until LONG press” behaviour.
        self.broadcast_state()

    def process_pico_line(self, port: str, line: str):
        parts = line.strip().split()
        if not parts:
            return

        if parts[0] == "EVT" and len(parts) == 3:
            try:
                btn = int(parts[1])
                kind = parts[2].upper()
                if btn in (0, 1, 2, 3) and kind in ("SINGLE", "DOUBLE", "LONG"):
                    self.handle_button_event(f"pico:{port}", btn, kind)
            except Exception:
                return

        elif parts[0] == "HELLO_ACK":
            print(f"[pico] HELLO_ACK from {port} -> syncing state")
            self.broadcast_state()

    def run_forever(self):
        def discover_loop():
            while True:
                self.ensure_picos()
                if self.socketio is not None:
                    self.socketio.emit("picos", self.pico_summary())
                    self.socketio.emit("state", self.state.to_dict())
                time.sleep(2.0)

        threading.Thread(target=discover_loop, daemon=True).start()

        self.broadcast_state()

        while True:
            evt = self.event_q.get()
            if evt.get("type") == "pico_line":
                self.process_pico_line(evt["port"], evt["line"])

# -----------------------------
# Flask App
# -----------------------------
app = Flask(__name__, static_folder="static", static_url_path="/static")

socketio = None
if SOCKETIO_OK:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

hub = Hub(socketio_obj=socketio)

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.get("/api/state")
def api_state():
    return jsonify(hub.state.to_dict())

@app.get("/api/picos")
def api_picos():
    return jsonify(hub.pico_summary())

@app.post("/api/evt")
def api_evt():
    data = request.get_json(force=True, silent=True) or {}
    try:
        btn = int(data.get("btn"))
        kind = str(data.get("kind", "")).upper()
        if btn in (0, 1, 2, 3) and kind in ("SINGLE", "DOUBLE", "LONG"):
            hub.handle_button_event("web", btn, kind)
            return jsonify({"ok": True})
    except Exception:
        pass
    return jsonify({"ok": False}), 400

if socketio is not None:
    @socketio.on("connect")
    def on_connect():
        emit("state", hub.state.to_dict())
        emit("picos", hub.pico_summary())

    @socketio.on("web_evt")
    def on_web_evt(data):
        try:
            btn = int(data.get("btn"))
            kind = str(data.get("kind", "")).upper()
            if btn in (0, 1, 2, 3) and kind in ("SINGLE", "DOUBLE", "LONG"):
                hub.handle_button_event("web", btn, kind)
        except Exception:
            return

def main():
    threading.Thread(target=hub.run_forever, daemon=True).start()
    print(f"[web] Listening on http://{HTTP_BIND}:{HTTP_PORT}")

    if socketio is not None:
        socketio.run(app, host=HTTP_BIND, port=HTTP_PORT)
    else:
        app.run(host=HTTP_BIND, port=HTTP_PORT, threaded=True)

if __name__ == "__main__":
    main()
