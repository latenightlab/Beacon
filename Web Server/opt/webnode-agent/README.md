# Webnode Agent (for Webserver VM)

This provides a local status/control API on the **Webserver VM** for NetPanel to consume.

## Units controlled
- {"MAP_UNIT"} defaults to `santa-server.service`
- {"CLOUDFLARED_UNIT"} defaults to `cloudflared.service`

## Install
```bash
sudo mkdir -p /opt/webnode-agent
sudo chown -R $USER:$USER /opt/webnode-agent
# copy webnode_agent.py + requirements.txt into /opt/webnode-agent

python3 -m venv /opt/webnode-agent/venv
/opt/webnode-agent/venv/bin/pip install -r /opt/webnode-agent/requirements.txt
```

## Environment
Create `/etc/webnode-agent.env`:

```ini
ADMIN_TOKEN=change-me-long-random
PING_TARGET=1.1.1.1

MAP_UNIT=santa-server.service
CLOUDFLARED_UNIT=cloudflared.service

# santa-server local base URL (where your map flask app listens)
SANTA_BASE=http://127.0.0.1:8000

# (optional) if santa-server admin endpoints use a different token
SANTA_ADMIN_TOKEN=change-me-long-random

PORT=8050
```

## systemd
Copy `systemd/webnode-agent.service` to `/etc/systemd/system/webnode-agent.service`:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now webnode-agent
sudo systemctl status webnode-agent
```

NetPanel should then reach it at: `http://192.168.196.5:8050/api/status`

## Important: santa-server update toggle endpoints
For the NetPanel “Map location updates” buttons to work, `santa-server` must implement:

- `GET  /api/admin/updates`
- `POST /api/admin/updates` body: `{"enabled": true|false}`

If you want, paste your current santa-server `app.py` and I’ll give you an exact patch.
