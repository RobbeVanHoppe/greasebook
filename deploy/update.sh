#!/usr/bin/env bash
# Pull the newest published image and restart GreaseBook only if it changed.
# Install once as a 5-minute systemd timer:   sudo bash deploy/update.sh --install
# Run by hand:                                 sudo bash deploy/update.sh
set -euo pipefail
DIR="${GREASEBOOK_DIR:-/opt/greasebook}"
UNIT=greasebook-update

if [[ "${1:-}" == "--install" ]]; then
  install -m 0755 "$(readlink -f "$0")" /usr/local/bin/greasebook-update
  cat > /etc/systemd/system/$UNIT.service <<EOF
[Unit]
Description=Update GreaseBook to the newest published image
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
Environment=GREASEBOOK_DIR=$DIR
ExecStart=/usr/local/bin/greasebook-update
EOF
  cat > /etc/systemd/system/$UNIT.timer <<EOF
[Unit]
Description=Check for a new GreaseBook image every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now $UNIT.timer
  echo "Installed. Check with: systemctl list-timers $UNIT.timer ; journalctl -u $UNIT"
  exit 0
fi

cd "$DIR"
before=$(docker compose images -q greasebook 2>/dev/null || true)
docker compose pull -q greasebook
docker compose up -d greasebook
after=$(docker compose images -q greasebook)
if [[ "$before" != "$after" ]]; then
  echo "GreaseBook updated: ${before:-none} -> $after"
  docker image prune -f >/dev/null
else
  echo "GreaseBook already up to date ($after)"
fi
