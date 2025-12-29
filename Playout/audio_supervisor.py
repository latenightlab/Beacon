#!/usr/bin/env python3
import json
import subprocess
import time
import requests

MOPIDY_URL = "http://localhost:6680/mopidy/rpc"
FALLBACK_FOLDER = "fallback"  # folder name under /var/lib/mopidy/media

# How often to check (seconds)
TICK = 3


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
            print("Mopidy error:", data["error"])
            return None
        return data.get("result")
    except Exception as e:
        print("Mopidy RPC error:", e)
        return None


def get_pipewire_active_external():
    """
    Returns True if any PipeWire sink input that is NOT mopidy is RUNNING.
    We detect by media.name or application.name containing 'shairport', 'raspotify',
    'gmediarender' etc. Adjust if needed.
    """

    try:
        # pw-dump outputs JSON
        result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3)
        data = json.loads(result.stdout)

        external_active = False

        for node in data:
            if node.get("type") != "PipeWire:Interface:Node":
                continue

            props = node.get("props", {})
            media_class = props.get("media.class", "")
            state = node.get("info", {}).get("state", "")

            # Only consider playback streams
            if media_class != "Stream/Output":
                continue

            app_name = props.get("application.name", "").lower()
            media_name = props.get("media.name", "").lower()

            is_mopidy = ("mopidy" in app_name) or ("mopidy" in media_name)

            if state == "running" and not is_mopidy:
                external_active = True
                break

        return external_active
    except Exception as e:
        print("PipeWire check error:", e)
        return False


def ensure_fallback_playing():
    # Get current track list
    current_track = mopidy_rpc("core.playback.get_current_track")
    state = mopidy_rpc("core.playback.get_state")

    if state == "playing" and current_track:
        # Something already playing; assume OK
        return

    # Build a fallback tracklist if needed
    tl_tracks = mopidy_rpc("core.tracklist.get_tl_tracks") or []
    if not tl_tracks:
        # Clear current list and add fallback folder
        mopidy_rpc("core.tracklist.clear")
        mopidy_rpc(
            "core.tracklist.add",
            {
                "uris": [f"file:///{FALLBACK_FOLDER}"]
            }
        )

    # Start playback
    mopidy_rpc("core.playback.play")


def pause_mopidy():
    state = mopidy_rpc("core.playback.get_state")
    if state == "playing":
        mopidy_rpc("core.playback.pause")


def main_loop():
    while True:
        external_active = get_pipewire_active_external()

        if external_active:
            # External source (AirPlay/Spotify/DLNA) is active → pause Mopidy
            print("External stream active → pausing Mopidy")
            pause_mopidy()
        else:
            # No external stream → ensure fallback playing
            print("No external stream → ensuring fallback playlist playing")
            ensure_fallback_playing()

        time.sleep(TICK)


if __name__ == "__main__":
    main_loop()
