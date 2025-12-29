#!/usr/bin/env python3
from flask import Flask, render_template, jsonify, request
import subprocess
import json
import requests

app = Flask(__name__)

MOPIDY_URL = "http://localhost:6680/mopidy/rpc"


def mopidy_rpc(method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {}
    }
    try:
        r = requests.post(MOPIDY_URL, json=payload, timeout=2)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return None
        return data.get("result")
    except Exception:
        return None


def get_pipewire_status():
    try:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3)
        data = json.loads(result.stdout)

        external_active = False
        external_sources = []

        for node in data:
            if node.get("type") != "PipeWire:Interface:Node":
                continue

            props = node.get("props", {})
            media_class = props.get("media.class", "")
            state = node.get("info", {}).get("state", "")

            if media_class != "Stream/Output":
                continue

            app_name = props.get("application.name", "Unknown")
            media_name = props.get("media.name", "")

            is_mopidy = ("mopidy" in app_name.lower()) or ("mopidy" in media_name.lower())

            if state == "running":
                if not is_mopidy:
                    external_active = True
                    external_sources.append(app_name)

        return {
            "external_active": external_active,
            "external_sources": list(set(external_sources))
        }
    except Exception:
        return {
            "external_active": False,
            "external_sources": []
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    # Mopidy state
    state = mopidy_rpc("core.playback.get_state") or "unknown"
    track = mopidy_rpc("core.playback.get_current_track") or {}

    title = track.get("name") if isinstance(track, dict) else None
    artist = None
    if isinstance(track, dict):
        artists = track.get("artists") or []
        if artists:
            artist = artists[0].get("name")

    pw_status = get_pipewire_status()

    return jsonify({
        "mopidy_state": state,
        "track_title": title,
        "track_artist": artist,
        "pipewire": pw_status
    })


@app.route("/api/mopidy/play", methods=["POST"])
def api_mopidy_play():
    mopidy_rpc("core.playback.play")
    return jsonify({"ok": True})


@app.route("/api/mopidy/pause", methods=["POST"])
def api_mopidy_pause():
    mopidy_rpc("core.playback.pause")
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
