from flask import Flask, request, jsonify, Response
import time

app = Flask(__name__)

# Simple shared secret for location updates
AUTH_TOKEN = "wefig24qoe9fnqunq08hnwf09dnxqp89r20hf93ndo"

# Stores last known location in memory
LAST_LOCATION = None


@app.post("/api/update-location")
def update_location():
    global LAST_LOCATION

    data = request.get_json(silent=True) or {}

    # Basic auth check
    if data.get("token") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    if "lat" not in data or "lon" not in data:
        return jsonify({"error": "missing lat/lon"}), 400

    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (ValueError, TypeError):
        return jsonify({"error": "invalid lat/lon"}), 400

    ts = data.get("timestamp", time.time())
    try:
        ts = float(ts)
    except (ValueError, TypeError):
        ts = time.time()

    LAST_LOCATION = {
        "lat": lat,
        "lon": lon,
        "timestamp": ts,
    }

    return jsonify({"status": "ok"})


@app.get("/api/location")
def get_location():
    if LAST_LOCATION is None:
        return jsonify({"error": "no data yet"}), 404
    return jsonify(LAST_LOCATION)


@app.get("/")
def index():
    html = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Santa Tracker | Pontypridd & Rhondda Round Table</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">

  <!-- Leaflet map CSS -->
  <link rel="stylesheet" href="/static/leaflet.css" />

  <style>
    html, body {
      height: 100%;
      margin: 0;
      padding: 0;
    }

    #map {
      width: 100%;
      height: 100%;
    }

    .leaflet-control-attribution {
      font-size: 10px;
    }

    /* ===== INFO BAR ===== */
    #info-bar {
      position: absolute;
      top: 10px;
      left: 50%;
      transform: translateX(-50%);
      background: rgba(0,0,0,0.6);
      color: #fff;
      padding: 10px 14px;
      border-radius: 10px;
      font-family: system-ui, sans-serif;
      font-size: 14px;
      z-index: 1000;
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 280px;
      text-align: center;
    }

    #info-top {
      display: flex;
      justify-content: center;
      gap: 14px;
      align-items: center;
    }

    #info-links {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-size: 13px;
    }

    #info-links a {
      color: #ffffff;
      text-decoration: none;
      opacity: 0.85;
    }

    #info-links a:hover {
      opacity: 1;
      text-decoration: underline;
    }

    /* ===== STATUS COLOURS ===== */
    .status-live { color: #1aa34a; font-weight: 800; }
    .status-stale { color: #e69500; font-weight: 800; }
    .status-offline { color: #cc0000; font-weight: 800; }
    .status-resting { color: #bbbbbb; font-weight: 800; }

    /* ===== WATERMARK ===== */
    #watermark {
      position: absolute;
      right: 10px;
      bottom: 10px;
      z-index: 1000;
      opacity: 0.6;
      width: 140px;
      height: auto;
      pointer-events: none; /* do not block map interaction */
      user-select: none;
    }
  </style>
</head>
<body>

<div id="map"></div>

<!-- ===== INFO BAR ===== -->
<div id="info-bar">
  <div id="info-top">
    <div>
      <strong>Status:</strong>
      <span id="live-status" class="status-resting">RESTING</span>
    </div>
    <div>
      <strong>Last updated:</strong>
      <span id="info-updated">No data yet</span>
    </div>
  </div>

  <!-- ===== LINKS ROW ===== -->
  <div id="info-links">
    <a href="#" target="_blank" rel="noopener">Donate</a>
    <a href="#" target="_blank" rel="noopener">Route</a>
    <a href="#" target="_blank" rel="noopener">Facebook</a>
    <a href="#" target="_blank" rel="noopener">Website</a>
  </div>
</div>

<!-- ===== MAP SCRIPTS ===== -->
<script src="/static/leaflet.js"></script>
<script>
  var map = L.map('map').setView([51.3779263, -3.1237549], 16);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19
  }).addTo(map);

  var santaIcon = L.icon({
    iconUrl: '/static/santa-sleigh.png',
    iconSize: [80, 60],
    iconAnchor: [30, 30]
  });

  var marker = L.marker(
    [51.3779263, -3.1237549],
    { icon: santaIcon }
  ).addTo(map);

  var infoUpdated = document.getElementById('info-updated');
  var liveStatus = document.getElementById('live-status');

  function setStatus(text, cls) {
    liveStatus.textContent = text;
    liveStatus.className = cls;
  }

  async function refreshLocation() {
    try {
      const resp = await fetch('/api/location');

      if (!resp.ok) {
        setStatus('RESTING', 'status-resting');
        return;
      }

      const data = await resp.json();

      marker.setLatLng([data.lat, data.lon]);
      map.setView([data.lat, data.lon], 16);

      const ts = new Date(data.timestamp * 1000);
      infoUpdated.textContent = ts.toLocaleTimeString();

      const age = (Date.now() - ts.getTime()) / 1000;

      if (age <= 60) setStatus('LIVE', 'status-live');
      else if (age <= 180) setStatus('AWAITING UPDATE', 'status-stale');
      else setStatus('OFFLINE', 'status-offline');

    } catch {
      setStatus('OFFLINE', 'status-offline');
    }
  }

  refreshLocation();
  setInterval(refreshLocation, 10000);
</script>

<!-- ===== WATERMARK IMAGE ===== -->
<img id="watermark" src="/static/watermark.png" alt="">

</body>
</html>
"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)