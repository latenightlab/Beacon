#!/usr/bin/env python3
"""
Webnode Agent - status/control endpoint for the Webserver VM.

Implements:
- GET  /api/status
- POST /api/service/<unit>/<action>   (start|stop|restart)
- POST /api/power/<action>            (reboot|shutdown)
- POST /api/map-updates               (enable/disable location updates in santa-server app)

Security:
- If ADMIN_TOKEN is set, requires X-Auth-Token == ADMIN_TOKEN for all POST actions.
- For GET /api/status: does not require token (so NetPanel can show status even if you later lock down control).
  You can change this if you prefer.
"""

from __future__ import annotations

import os
import time
import shutil
import subprocess
from typing import Dict, Any, Tuple, Optional

import requests
from flask import Flask, jsonify, request, abort

app = Flask(__name__)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
PING_TARGET = os.environ.get("PING_TARGET", "1.1.1.1")

# Your units (as requested)
MAP_UNIT = os.environ.get("MAP_UNIT", "santa-server.service")
CLOUDFLARED_UNIT = os.environ.get("CLOUDFLARED_UNIT", "cloudflared.service")

# Where santa-server listens locally
SANTA_BASE = os.environ.get("SANTA_BASE", "http://127.0.0.1:8000")

# If santa-server's admin toggle endpoint requires a different token, set it here.
# If you use the same token everywhere, you can leave this unset (it will reuse ADMIN_TOKEN).
SANTA_ADMIN_TOKEN = os.environ.get("SANTA_ADMIN_TOKEN", ADMIN_TOKEN)

UNITS = [MAP_UNIT, CLOUDFLARED_UNIT]


def _run(cmd, timeout=3) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _require_admin():
    if ADMIN_TOKEN:
        if request.headers.get("X-Auth-Token", "") != ADMIN_TOKEN:
            abort(403)
    else:
        # Safe default if no token configured: only allow localhost
        if request.remote_addr not in ("127.0.0.1", "::1"):
            abort(403)


def systemd_is_active(unit: str) -> Dict[str, Any]:
    rc, out, err = _run(["systemctl", "is-active", unit])
    return {"unit": unit, "active": rc == 0, "state": (out or err)}


def systemd_status_tail(unit: str, lines_to_fetch: int = 12, tail_lines: int = 3) -> str:
    rc, out, err = _run(["systemctl", "status", unit, "--no-pager", "-n", str(lines_to_fetch)], timeout=3)
    text = out or err
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-tail_lines:]) if lines else ""


def internet_ping_status() -> Dict[str, Any]:
    target = PING_TARGET
    if not shutil.which("ping"):
        return {"ok": False, "target": target, "latency_ms": None, "detail": "no ping binary"}

    try:
        rc, out, err = _run(["ping", "-c", "1", "-W", "1", target], timeout=2)
        if rc != 0:
            return {"ok": False, "target": target, "latency_ms": None, "detail": (err or out or "ping failed")[:200]}

        import re
        m = re.search(r"time[=<]([\d\.]+)\s*ms", out)
        latency = float(m.group(1)) if m else None
        return {"ok": True, "target": target, "latency_ms": latency, "detail": "ping ok"}
    except Exception as e:
        return {"ok": False, "target": target, "latency_ms": None, "detail": f"{type(e).__name__}: {e}"}


def get_santa_updates() -> Dict[str, Any]:
    """Calls santa-server admin endpoint (must exist in santa-server app)."""
    try:
        headers = {}
        if SANTA_ADMIN_TOKEN:
            headers["X-Auth-Token"] = SANTA_ADMIN_TOKEN
        r = requests.get(f"{SANTA_BASE}/api/admin/updates", headers=headers, timeout=1.5)
        if not r.ok:
            return {"error": f"http {r.status_code}", "detail": r.text[:200]}
        return r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def set_santa_updates(enabled: bool) -> Tuple[int, Dict[str, Any]]:
    try:
        headers = {}
        if SANTA_ADMIN_TOKEN:
            headers["X-Auth-Token"] = SANTA_ADMIN_TOKEN
        r = requests.post(
            f"{SANTA_BASE}/api/admin/updates",
            headers=headers,
            json={"enabled": enabled},
            timeout=2.0,
        )
        if r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
        else:
            body = {"raw": r.text[:300]}
        return r.status_code, body
    except Exception as e:
        return 502, {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/status")
def api_status():
    services: Dict[str, Any] = {}
    for unit in UNITS:
        s = systemd_is_active(unit)
        s["tail"] = systemd_status_tail(unit)
        services[unit] = s

    return jsonify({
        "services": services,
        "internet": internet_ping_status(),
        "map_updates": get_santa_updates(),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()),
    })


@app.post("/api/service/<path:unit>/<action>")
def api_service_action(unit: str, action: str):
    _require_admin()

    # Restrict to allowed units
    if unit not in UNITS:
        return jsonify({"ok": False, "stderr": "unit not permitted"}), 403
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "stderr": "invalid action"}), 400

    rc, out, err = _run(["systemctl", action, unit], timeout=12)
    return jsonify({"ok": rc == 0, "rc": rc, "stdout": out, "stderr": err}), (200 if rc == 0 else 500)


@app.post("/api/power/<action>")
def api_power(action: str):
    _require_admin()

    if action not in ("reboot", "shutdown"):
        return jsonify({"ok": False, "stderr": "invalid action"}), 400

    cmd = ["systemctl", "reboot"] if action == "reboot" else ["systemctl", "poweroff"]
    rc, out, err = _run(cmd, timeout=2)
    return jsonify({"ok": rc == 0, "rc": rc, "stdout": out, "stderr": err}), (200 if rc == 0 else 500)


@app.post("/api/map-updates")
def api_map_updates():
    _require_admin()

    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", None)
    if not isinstance(enabled, bool):
        return jsonify({"ok": False, "stderr": "enabled must be boolean"}), 400

    status, body = set_santa_updates(enabled)
    return jsonify({"ok": status < 400, "status_code": status, "body": body}), status


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8050")))
