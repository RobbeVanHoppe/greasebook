#!/usr/bin/env bash
# GreaseBook -- bare-metal install (no containers).
# Ideal inside an LXC container: the LXC *is* the isolation layer.
#
#   sudo ./install.sh
#
# Creates a venv, installs deps, sets up a systemd service that starts at
# boot and restarts on failure. Data lives in /var/lib/greasebook.
set -euo pipefail

APP_DIR=/opt/greasebook
DATA_DIR=/var/lib/greasebook
SVC_USER=greasebook
PORT=8000

if [ "$EUID" -ne 0 ]; then
    echo "Run me with sudo." >&2
    exit 1
fi

echo ">> Installing python3-venv (if missing)"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

echo ">> Creating service user '$SVC_USER'"
id -u "$SVC_USER" >/dev/null 2>&1 || \
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"

echo ">> Copying app to $APP_DIR"
mkdir -p "$APP_DIR"
cp -r app.py templates static requirements.txt "$APP_DIR/"

echo ">> Building virtualenv"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo ">> Preparing data dir $DATA_DIR"
mkdir -p "$DATA_DIR"
chown -R "$SVC_USER:$SVC_USER" "$DATA_DIR" "$APP_DIR"

echo ">> Installing systemd service"
cat > /etc/systemd/system/greasebook.service <<UNIT
[Unit]
Description=GreaseBook garage maintenance ledger
After=network.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$APP_DIR
Environment=GREASEBOOK_DATA=$DATA_DIR
# Uncomment to require a password:
# Environment=GREASEBOOK_PASSWORD=changeme
ExecStart=$APP_DIR/venv/bin/waitress-serve --host=0.0.0.0 --port=$PORT app:app
Restart=always
RestartSec=3

# modest hardening -- it only ever needs to write its own data dir
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now greasebook
sleep 1

echo
if systemctl is-active --quiet greasebook; then
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "GreaseBook is running:  http://${IP:-<this-host>}:$PORT"
    echo
    echo "  status:   systemctl status greasebook"
    echo "  logs:     journalctl -u greasebook -f"
    echo "  restart:  systemctl restart greasebook"
    echo "  data:     $DATA_DIR   (back this up -- it is your service history)"
else
    echo "Service failed to start. Check:  journalctl -u greasebook -n 40"
    exit 1
fi
