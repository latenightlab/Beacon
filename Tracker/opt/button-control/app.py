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
# If you create udev symlinks, set: ["/dev/pico-panel-1", "/dev/pico-panel-2"]
SERIAL_PORTS: List[str] = []

SERIAL_BAUD = 115200
SERIAL_TIMEOUT = 0.1
PICO_RECONNECT_INTERVAL_S = 2.0
PICO_PING_INTERVAL_S = 1.0

# Relay hat (I2C expander style) - not used for relays 0/1 in this raw-i2cset build
I2C_BUS = 1
I2C_ADDR = 0x20
RELAY_ACTIVE_LOW = True

# systemd service to control
PUBLISHER_SERVICE = "santa-publisher.service"

# -----------------------------
# Helpers
# -----------------------------
def run_cmd(cmd: List[str]) -> bool:
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def i2cset_cmd(bus: int, addr: int, reg: int, value: int) -> None:
    # Uses the i2c-tools command exactly (works even if you don't want smbus2)
    run_cmd([
        "i2cset",
        "-y",
        str(bus),
        hex(addr),
        hex(reg),
        hex(value),
    ])

def amixer_set_digital(percent: int) -> None:
    run_cmd(["amixer", "sset", "Digital", f"{int(percent)}%"])

def ssh_amixer_set_digital(host: str, percent: int, user: str = "ctrlaudio") -> None:
    # Fail-fast + non-interactive
    run_cmd([
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=2",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        "sudo", "amixer", "sset", "Digital", f"{int(percent)}%"
    ])

def systemctl_restart(service: str) -> None:
    run_cmd(["systemctl", "restart", service])

def systemctl_stop(service: str) -> None:
    run_cmd(["systemctl", "stop", service])

def systemctl_is_active(service: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", service], check=False, capture_output=True, text=True)
        return r.stdout.strip() == "active"
    except Exception:
        return False

# -----------------------------
# Relay controller (I2C expander-style)
# (kept for future expansion; not used for relays 0/1 in raw-i2cset mode)
# -----------------------------
class RelayController:
    """
    Assumes an 8-bit I2C GPIO expander driving relays on bits 0..7.
    We keep an output byte and write it on each change.
    """
    def __init__(self, bus: int, addr: int, active_low: bool):
        self.bus_num = bus
        self.addr = addr
        self.active_low = active_low
        self._lock = threading.Lock()
        self._out = 0x00  # logical ON bits

        try:
            from smbus2 import SMBus  # type: ignore
            self._SMBus = SMBus
            self.available = True
        except Exception:
            self._SMBus = None
            self.available = False
            print("[relay] WARNING: smbus2 not available, relay control disabled")

        self.all_off()

    def _write_byte(self, value: int) -> None:
        if not self.available:
            return
        try:
            with self._SMBus(self.bus_num) as bus:
                bus.write_byte(self.addr, value & 0xFF)
        except Exception as e:
            print(f"[relay] I2C write failed: {e}")

    def _apply(self) -> None:
        if self.active_low:
            hw = (~self._out) & 0xFF
        else:
            hw = self._out & 0xFF
        self._write_byte(hw)

    def set(self, relay_id: int, on: bool) -> None:
        if relay_id < 0 or relay_id > 7:
            return
        with self._lock:
            if on:
                self._out |= (1 << relay_id)
            else:
                self._out &= ~(1 << relay_id)
            self._apply()
        print(f"[relay] {relay_id} => {'ON' if on else 'OFF'}")

    def get(self, relay_id: int) -> bool:
        with self._lock:
            return bool(self._out & (1 << relay_id))

    def all_off(self) -> None:
        with self._lock:
            self._out = 0x00
            self._apply()
        print("[relay] ALL OFF")

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

    relay0: bool = False  # Button 0 logical state (raw i2cset)
    relay1: bool = False  # Button 1 logical state (raw i2cset)
    publisher_active: bool = False

    def to_dict(self):
        return {
            "leds": {
                str(i): {"mode": self.leds[i].mode, "rgb": list(self.leds[i].rgb)}
                for i in self.leds
            },
            "relay0": self.relay0,
            "relay1": self.relay1,
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
        self._flash_timers: Dict[int, threading.Timer] = {}
        self._steady_led: Dict[int, LedState] = {i: LedState() for i in range(4)}
        self.relay = RelayController(I2C_BUS, I2C_ADDR, RELAY_ACTIVE_LOW)

        # initial publisher state
        self.state.publisher_active = systemctl_is_active(PUBLISHER_SERVICE)

    def _set_led_now(self, idx: int, mode: str, rgb: Tuple[int, int, int]):
        with self.state_lock:
            self.state.leds[idx].mode = mode
            self.state.leds[idx].rgb = rgb

    def set_led_steady(self, idx: int, mode: str, rgb: Tuple[int, int, int]):
        # store intended steady state
        with self.state_lock:
            self._steady_led[idx] = LedState(mode=mode, rgb=rgb)
            self.state.leds[idx].mode = mode
            self.state.leds[idx].rgb = rgb
        self.broadcast_state()

    def flash_led_pulse(self, idx: int, rgb: Tuple[int, int, int], seconds: float = 0.6):
        # cancel any existing timer for this LED
        t = self._flash_timers.get(idx)
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

        # set flashing immediately
        self._set_led_now(idx, "FLASH", rgb)
        self.broadcast_state()

        # schedule revert to last steady state
        def revert():
            with self.state_lock:
                steady = self._steady_led.get(idx, LedState())
                self.state.leds[idx].mode = steady.mode
                self.state.leds[idx].rgb = steady.rgb
            self.broadcast_state()

        timer = threading.Timer(seconds, revert)
        timer.daemon = True
        self._flash_timers[idx] = timer
        timer.start()

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

        # Button 0: raw I2C control
        # SINGLE = i2cset -y 1 0x10 0x01 0xFF
        # LONG   = i2cset -y 1 0x10 0x01 0x00
        if btn == 0:
            if kind == "SINGLE":
                i2cset_cmd(1, 0x10, 0x01, 0xFF)
                with self.state_lock:
                    self.state.relay0 = True
                    self.state.leds[0].mode = "SOLID"
                    self.state.leds[0].rgb = (0, 255, 0)

            elif kind == "LONG":
                i2cset_cmd(1, 0x10, 0x01, 0x00)
                with self.state_lock:
                    self.state.relay0 = False
                    self.state.leds[0].mode = "OFF"
                    self.state.leds[0].rgb = (0, 0, 0)

        # Button 1: raw I2C control (same style as button 0)
        elif btn == 1:
            if kind == "SINGLE":
                i2cset_cmd(1, 0x10, 0x02, 0xFF)
                with self.state_lock:
                    self.state.relay1 = True
                    self.state.leds[1].mode = "SOLID"
                    self.state.leds[1].rgb = (0, 255, 0)

            elif kind == "LONG":
                i2cset_cmd(1, 0x10, 0x02, 0x00)
                with self.state_lock:
                    self.state.relay1 = False
                    self.state.leds[1].mode = "OFF"
                    self.state.leds[1].rgb = (0, 0, 0)

        # Button 2: remote ALSA Digital volume
        elif btn == 2:
            if kind == "SINGLE":
                ssh_amixer_set_digital("192.168.196.11", 60, user="ctrlaudio")
                # pulse flash then revert to OFF
                self.set_led_steady(2, "OFF", (0, 0, 0))
                self.flash_led_pulse(2, (0, 120, 255), seconds=0.6)

            elif kind == "LONG":
                ssh_amixer_set_digital("192.168.196.11", 100, user="ctrlaudio")
                self.set_led_steady(2, "OFF", (0, 0, 0))
                self.flash_led_pulse(2, (0, 120, 255), seconds=0.6)

        # Button 3: systemd service control
        elif btn == 3:
            if kind == "SINGLE":
                systemctl_restart(PUBLISHER_SERVICE)
                active = systemctl_is_active(PUBLISHER_SERVICE)
                with self.state_lock:
                    self.state.publisher_active = active
                    self.state.leds[3].mode = "FLASH"
                    self.state.leds[3].rgb = (255, 255, 255)

            elif kind == "LONG":
                systemctl_stop(PUBLISHER_SERVICE)
                active = systemctl_is_active(PUBLISHER_SERVICE)
                with self.state_lock:
                    self.state.publisher_active = active
                    self.state.leds[3].mode = "SOLID" if active else "OFF"
                    self.state.leds[3].rgb = (255, 255, 255)

        # ---- sync "truth" where it is safe to do so ----
        # IMPORTANT:
        # - relay0 and relay1 are controlled via raw i2cset, so there is no reliable read-back here.
        #   Treat relay0/relay1 as *commanded state* and do NOT overwrite LEDs 0/1.
        # - Publisher state can be queried safely.
        with self.state_lock:
            if btn != 3:
                self.state.publisher_active = systemctl_is_active(PUBLISHER_SERVICE)
                self.state.leds[3].mode = "SOLID" if self.state.publisher_active else "OFF"
                self.state.leds[3].rgb = (255, 255, 255)

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
