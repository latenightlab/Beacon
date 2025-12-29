#!/usr/bin/env python3
import time
import requests
import logging

MOPIDY_RPC = "http://127.0.0.1:6680/mopidy/rpc"
FALLBACK_DIR_URI = "file:///var/lib/mopidy/media/fallback"
CHECK_INTERVAL = 5  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [supervisor] %(message)s"
)


def mopidy_rpc(method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {}
    }
    try:
        r = requests.post(MOPIDY_RPC, json=payload, timeout=2)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            logging.warning("Mopidy error on %s: %s", method, data["error"])
            return None
        return data.get("result")
    except Exception as e:
        logging.warning("Mopidy RPC error on %s: %s", method, e)
        return None


def is_fallback_tracklist(tl_tracks):
    """Return True if ALL tracks in the tracklist are from the fallback directory."""
    uris = []
    for tl in tl_tracks:
        track = tl.get("track") or {}
        uri = track.get("uri")
        if uri:
            uris.append(uri)

    if not uris:
        return False

    return all(uri.startswith(FALLBACK_DIR_URI) for uri in uris)


def start_fallback(force_clear=False):
    """Start fallback playback from the fallback directory."""
    if force_clear:
        logging.info("Clearing tracklist before starting fallback")
        mopidy_rpc("core.tracklist.clear")

    # Browse fallback folder and get tracks
    children = mopidy_rpc("core.library.browse", {"uri": FALLBACK_DIR_URI}) or []
    uris = [c["uri"] for c in children if c.get("type") == "track"]

    if not uris:
        logging.warning("No tracks found in fallback directory!")
        return

    logging.info("Adding %d fallback tracks", len(uris))
    mopidy_rpc("core.tracklist.add", {"uris": uris})
    logging.info("Starting fallback playback")
    mopidy_rpc("core.playback.play")


def ensure_fallback_playing():
    """Keep fallback going when Mopidy is idle, but don't fight user actions."""
    state = mopidy_rpc("core.playback.get_state")
    if state is None:
        logging.info("Mopidy not reachable yet")
        return

    logging.info("Mopidy state: %s", state)

    # If paused, user pressed pause → do nothing
    if state == "paused":
        logging.info("User paused playback; leaving it paused")
        return

    # If already playing anything, leave it alone
    if state == "playing":
        return

    # state is 'stopped' or something unexpected. Look at the tracklist.
    tl_tracks = mopidy_rpc("core.tracklist.get_tl_tracks") or []

    if tl_tracks:
        # There are tracks queued. Check if they are ALL fallback tracks.
        if is_fallback_tracklist(tl_tracks):
            logging.info("Stopped with only fallback tracks queued; restarting fallback")
            mopidy_rpc("core.playback.play")
        else:
            # User has their own queue; don't interfere
            logging.info("Stopped with non-fallback tracks queued; not starting fallback")
        return

    # No tracks queued at all and stopped → truly idle → start fallback fresh
    logging.info("No tracks queued; starting fallback")
    start_fallback(force_clear=False)


def main():
    logging.info("Audio supervisor started")
    while True:
        ensure_fallback_playing()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
