#!/usr/bin/env python3
"""
NetPanel - Network Control Panel (Aggregator)
Runs on the Webserver VM. It:
- Polls status agents on Tracker-Pi, Playout-Pi, and Webserver Agent
- Shows connectivity and simple host stats
- Proxies service control + power actions to each node agent
- Optionally toggles "location updates enabled" via Webserver Agent
"""

from __future__ import annotations

import os
import time
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple

import requests
from flask import Flask, jsonify, request, abort, send_from_directory

app = Flask(__name__, static_folder="static")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
PING_TARGET = os.environ.get("PING_TARGET", "1.1.1.1")

TRACKER_BASE = os.environ.get("TRACKER_BASE", "http://192.168.196.10")
PLAYOUT_BASE = os.environ.get("PLAYOUT_BASE", "http://192.168.196.11")
WEBSERVER_AGENT_BASE = os.environ.get("WEBSERVER_AGENT_BASE", "http://192.168.196.5:8050")

NODE_TOKEN_TRACKER = os.environ.get("NODE_TOKEN_TRACKER", ADMIN_TOKEN)
NODE_TOKEN_PLAYOUT = os.environ.get("NODE_TOKEN_PLAYOUT", ADMIN_TOKEN)
NODE_TOKEN_WEBSERVER = os.environ.get("NODE_TOKEN_WEBSERVER", ADMIN_TOKEN)

STATUS_TIMEOUT = float(os.environ.get("STATUS_TIMEOUT", "2.5"))
CONTROL_TIMEOUT = float(os.environ.get("CONTROL_TIMEOUT", "8.0"))
CACHE_TTL = float(os.environ.get("CACHE_TTL", "1.5"))


@dataclass
class Node:
    key: str
    name: str
    base_url: str
    token: str


NODES: Dict[str, Node] = {
    "tracker": Node("tracker", "Tracker-Pi", TRACKER_BASE.rstrip("/"), NODE_TOKEN_TRACKER),
    "playout": Node("playout", "Playout-Pi", PLAYOUT_BASE.rstrip("/"), NODE_TOKEN_PLAYOUT),
    "webserver": Node("webserver", "Webserver", WEBSERVER_AGENT_BASE.rstrip("/"), NODE_TOKEN_WEBSERVER),
}

_status_cache: Dict[str, Tuple[float, Any]] = {}


def _is_authorized(req) -> bool:
    if ADMIN_TOKEN:
        return req.headers.get("X-Auth-Token", "") == ADMIN_TOKEN
    return req.remote_addr in ("127.0.0.1", "::1")


def _run(cmd, timeout=2.5) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _ping(host: str) -> Dict[str, Any]:
    if not shutil.which("ping"):
        return {"ok": False, "latency_ms": None, "detail": "ping binary not found"}
    try:
        rc, out, err = _run(["ping", "-c", "1", "-W", "1", host], timeout=2.0)
        if rc != 0:
            return {"ok": False, "latency_ms": None, "detail": (err or out or "ping failed")[:200]}
        import re
        m = re.search(r"time[=<]([\d\.]+)\s*ms", out)
        latency = float(m.group(1)) if m else None
        return {"ok": True, "latency_ms": latency, "detail": "ok"}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "detail": f"{type(e).__name__}: {e}"}


def _fetch_node_status(node: Node) -> Dict[str, Any]:
    url = f"{node.base_url}/api/status"
    headers = {}
    if node.token:
        headers["X-Auth-Token"] = node.token
    try:
        r = requests.get(url, headers=headers, timeout=STATUS_TIMEOUT)
        if not r.ok:
            return {"ok": False, "http_status": r.status_code, "error": r.text[:300]}
        return {"ok": True, "data": r.json()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _get_cached_status(node: Node) -> Dict[str, Any]:
    now = time.time()
    cached = _status_cache.get(node.key)
    if cached and (now - cached[0]) < CACHE_TTL:
        return cached[1]
    payload = _fetch_node_status(node)
    _status_cache[node.key] = (now, payload)
    return payload


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def api_config():
    return jsonify({
        "nodes": {k: {"name": n.name, "base_url": n.base_url} for k, n in NODES.items()},
        "ping_target": PING_TARGET,
        "control_requires_token": bool(ADMIN_TOKEN),
    })


@app.get("/api/summary")
def api_summary():
    summary: Dict[str, Any] = {
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()),
        "panel_to_internet": _ping(PING_TARGET),
        "nodes": {},
    }
    for key, node in NODES.items():
        host = node.base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        summary["nodes"][key] = {
            "name": node.name,
            "base_url": node.base_url,
            "panel_ping": _ping(host),
            "agent_status": _get_cached_status(node),
        }
    return jsonify(summary)


def _proxy_post(node: Node, path: str, json_body: Optional[dict] = None) -> Tuple[int, Dict[str, Any]]:
    url = f"{node.base_url}{path}"
    headers = {}
    if node.token:
        headers["X-Auth-Token"] = node.token
    try:
        r = requests.post(url, headers=headers, json=json_body, timeout=CONTROL_TIMEOUT)
        if r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
        else:
            body = {"raw": r.text[:500]}
        return r.status_code, body
    except Exception as e:
        return 502, {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/node/<node_key>/service/<path:unit>/<action>")
def api_node_service(node_key: str, unit: str, action: str):
    if not _is_authorized(request):
        abort(403)
    node = NODES.get(node_key)
    if not node:
        return jsonify({"ok": False, "error": "unknown node"}), 404
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "invalid action"}), 400
    status, body = _proxy_post(node, f"/api/service/{unit}/{action}")
    return jsonify({"proxy_http_status": status, "body": body}), status


@app.post("/api/node/<node_key>/power/<action>")
def api_node_power(node_key: str, action: str):
    if not _is_authorized(request):
        abort(403)
    node = NODES.get(node_key)
    if not node:
        return jsonify({"ok": False, "error": "unknown node"}), 404
    if action not in ("reboot", "shutdown"):
        return jsonify({"ok": False, "error": "invalid action"}), 400
    status, body = _proxy_post(node, f"/api/power/{action}")
    return jsonify({"proxy_http_status": status, "body": body}), status


@app.post("/api/webserver/map-updates")
def api_webserver_map_updates():
    if not _is_authorized(request):
        abort(403)
    node = NODES["webserver"]
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", None)
    if not isinstance(enabled, bool):
        return jsonify({"ok": False, "error": "enabled must be boolean"}), 400
    status, body = _proxy_post(node, "/api/map-updates", json_body={"enabled": enabled})
    return jsonify({"proxy_http_status": status, "body": body}), status


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8060")))
