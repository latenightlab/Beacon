# NetPanel (Network Control Panel)

## What it does
NetPanel runs on your **Webserver VM** and aggregates status/control from:
- Tracker-Pi (192.168.196.10)
- Playout-Pi (192.168.196.11)
- Webserver Agent (on the same VM, default port 8050)

It provides:
- Connectivity (ping from the panel VM to each node)
- Node agent status (services, node internet ping, tracker GPS fix, etc.)
- Service start/stop/restart (proxied to node agents)
- Reboot/shutdown (proxied to node agents)
- Toggle "location updates enabled" (via webserver agent)

## Install (Webserver VM)
```bash
sudo mkdir -p /opt/netpanel
sudo chown -R $USER:$USER /opt/netpanel
# copy app.py, static/, requirements.txt, README.md into /opt/netpanel

python3 -m venv /opt/netpanel/venv
/opt/netpanel/venv/bin/pip install -r /opt/netpanel/requirements.txt
```

## Environment
Create `/etc/netpanel.env`:

```ini
ADMIN_TOKEN=change-me-long-random

TRACKER_BASE=http://192.168.196.10
PLAYOUT_BASE=http://192.168.196.11
WEBSERVER_AGENT_BASE=http://192.168.196.5:8050

NODE_TOKEN_TRACKER=change-me-long-random
NODE_TOKEN_PLAYOUT=change-me-long-random
NODE_TOKEN_WEBSERVER=change-me-long-random

PING_TARGET=1.1.1.1
PORT=8060
```

If you use one shared token everywhere, you can set `ADMIN_TOKEN` and omit the NODE_TOKEN_* vars.

## Run (manual)
```bash
sudo -E /opt/netpanel/venv/bin/python /opt/netpanel/app.py
```

Open: `http://192.168.196.5:8060`

## systemd
Copy `systemd/netpanel.service` to `/etc/systemd/system/netpanel.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now netpanel
sudo systemctl status netpanel
```

## Webserver agent
NetPanel expects a webserver agent at `http://192.168.196.5:8050` that implements:
- `GET /api/status`
- `POST /api/service/<unit>/<action>`
- `POST /api/power/<action>`
- `POST /api/map-updates` (optional toggle)

If you want, tell me what your map Flask systemd unit is called and I’ll generate the exact webserver agent + systemd unit for your VM too.
