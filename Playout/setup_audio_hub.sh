#!/usr/bin/env bash
set -e

echo "=== Update system ==="
sudo apt update
sudo apt -y upgrade

echo "=== Install PipeWire + audio bits ==="
sudo apt -y install \
  pipewire pipewire-audio pipewire-pulse wireplumber \
  alsa-utils

# Enable PipeWire audio in place of PulseAudio
systemctl --user --now enable pipewire pipewire-pulse wireplumber || true

echo "=== Install core audio services ==="
sudo apt -y install \
  shairport-sync \
  gmediarender \
  mopidy \
  python3-pip \
  python3-venv \
  git \
  curl

echo "=== Install Raspotify (Spotify Connect) ==="
curl -sL https://dtcooper.github.io/raspotify/install.sh | sh

echo "=== Install Mopidy extensions (Iris) ==="
sudo python3 -m pip install --break-system-packages Mopidy-Iris

echo "=== Create Mopidy media dir for fallback ==="
sudo mkdir -p /var/lib/mopidy/media/fallback
# sudo chown -R mopidy:mopidy /var/lib/mopidy
sudo chown -R mopidy:audio /var/lib/mopidy

echo "=== Enable services ==="
sudo systemctl enable --now shairport-sync
sudo systemctl enable --now gmediarender
sudo systemctl enable --now mopidy
sudo systemctl enable --now raspotify

echo "=== Install Python venv for dashboard + supervisor ==="
sudo mkdir -p /opt/audio-hub
sudo chown "$USER":"$USER" /opt/audio-hub

cd /opt/audio-hub
python3 -m venv venv
source venv/bin/activate
pip install flask requests

echo "=== Done base install. Next steps: configs and services. ==="
