#!/bin/bash
# Called by librespot via --onevent / LIBRESPOT_ONEVENT
# PLAYER_EVENT is provided by librespot: start / stop / change / etc.

RPC="http://127.0.0.1:6680/mopidy/rpc"

stop_mopidy() {
  curl -s -X POST "$RPC" \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"core.playback.stop","params":{}}' >/dev/null
}

case "$PLAYER_EVENT" in
  start|playing)
    stop_mopidy
    ;;
esac

