from flask import Flask, request, jsonify, Response
import time
import os

app = Flask(__name__)

AUTH_TOKEN = "SECRET_TOKEN"
LAST_LOCATION = None




ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
UPDATES_ENABLED = True  # when False, /api/update-location returns 503 but server stays online

def is_admin(req):
    # If ADMIN_TOKEN set, require it. Otherwise only allow localhost.
    if ADMIN_TOKEN:
        return req.headers.get("X-Auth-Token", "") == ADMIN_TOKEN
    return req.remote_addr in ("127.0.0.1", "::1")

@app.post("/api/update-location")
def update_location():
    global LAST_LOCATION, UPDATES_ENABLED

    if not UPDATES_ENABLED:
        return jsonify({"error": "updates_disabled"}), 503

    data = request.get_json(silent=True) or {}

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



@app.get("/api/admin/updates")
def get_updates_enabled():
    if not is_admin(request):
        return jsonify({"error": "forbidden"}), 403
    return jsonify({"updates_enabled": UPDATES_ENABLED})


@app.post("/api/admin/updates")
def set_updates_enabled():
    global UPDATES_ENABLED
    if not is_admin(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", None)
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be boolean"}), 400

    UPDATES_ENABLED = enabled
    return jsonify({"ok": True, "updates_enabled": UPDATES_ENABLED})




@app.get("/")
def index():
    html = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Santa Tracker | Pontypridd & Rhondda Round Table</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="/static/leaflet.css" />
  <style>
    html, body { height: 100%; margin: 0; padding: 0; }
    #map { width: 100%; height: 100%; }
    .leaflet-control-attribution { font-size: 10px; }

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

    .status-live { color: #1aa34a; font-weight: 800; }
    .status-stale { color: #e69500; font-weight: 800; }
    .status-offline { color: #cc0000; font-weight: 800; }
    .status-resting { color: #bbbbbb; font-weight: 800; }
  

    /* ===== WATERMARK ===== */
#watermark {
  position: fixed;
  right: 12px;
  bottom: 12px;
  z-index: 9999;
  width: 160px;
  height: auto;
  opacity: 0.7;
  pointer-events: none;
}
  </style>
</head>
<body>

<div id="map"></div>

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
    <a href="https://checkout.square.site/merchant/MLNWA748BSEAE/checkout/2CVQUP4VULUZ7BV6VTME4PYP" target="_blank" rel="noopener">Donate</a>
    <a href="#" target="_blank" rel="noopener">Route</a>
    <a href="https://www.facebook.com/PontypriddRhonddaRoundTable" target="_blank" rel="noopener">Facebook</a>
    <a href="#" target="_blank" rel="noopener">Website</a>
  </div>
</div>

<script src="/static/leaflet.js"></script>
<script>
  var map = L.map('map').setView([68.4970119,27.6102971], 16);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19
  }).addTo(map);

  var santaIcon = L.icon({
    iconUrl: '/static/santa-sleigh.png',
    iconSize: [80, 60],
    iconAnchor: [30, 30]
  });

  var marker = L.marker([68.4970119,27.6102971], { icon: santaIcon }).addTo(map);

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
<img src="/static/watermark.png" id="watermark" alt="">
</body>
</html>
"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

