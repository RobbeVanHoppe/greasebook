#!/usr/bin/env bash
# One-command server setup for the published GreaseBook image:
#   curl -fsSL https://raw.githubusercontent.com/RobbeVanHoppe/greasebook/main/deploy/setup.sh | sudo bash
# Installs into /opt/greasebook, starts the container on port 8000 and enables
# the 5-minute auto-update timer. Safe to run again. Needs Docker with compose.
set -euo pipefail
DIR="${GREASEBOOK_DIR:-/opt/greasebook}"
RAW=https://raw.githubusercontent.com/RobbeVanHoppe/greasebook/main/deploy

command -v docker >/dev/null || { echo "Docker is not installed. Install it first: https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "The docker compose plugin is missing (package docker-compose-plugin)."; exit 1; }

mkdir -p "$DIR/greasebook-data"
cd "$DIR"
curl -fsSLo docker-compose.yml "$RAW/docker-compose.yml"
curl -fsSLo update.sh "$RAW/update.sh"
chmod +x update.sh

# carry over data from a bare-metal install.sh setup, once
if [[ -f /var/lib/greasebook/garage.db && ! -f greasebook-data/garage.db ]]; then
  systemctl disable --now greasebook 2>/dev/null || true
  cp -a /var/lib/greasebook/. greasebook-data/
  echo "Copied your existing data from /var/lib/greasebook (old service stopped)."
fi

docker compose pull -q
docker compose up -d
GREASEBOOK_DIR="$DIR" bash update.sh --install
echo "GreaseBook is running: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000"
