#!/usr/bin/env bash
set -euo pipefail

# Install the PZ Tambayan bot as a systemd service.
# Run from anywhere; auto-detects the repo layout (<repo>/bot).
#
# Usage:
#   sudo ./install-service.sh            # bot dir = <repo>/bot
#   sudo ./install-service.sh /root/bot  # explicit bot dir

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOT_DIR="${1:-$REPO_DIR/bot}"
SERVICE_NAME="pz-tambayan-bot"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

if [ ! -f "$BOT_DIR/run.sh" ]; then
    echo "ERROR: run.sh not found in $BOT_DIR" >&2
    echo "Usage: $0 [bot-dir]   (default: $REPO_DIR/bot)" >&2
    exit 1
fi

# Pre-create the venv so the first service start doesn't run pip install under systemd.
if [ ! -x "$BOT_DIR/.venv/bin/python" ]; then
    echo "[install] Creating venv + installing requirements..."
    python3 -m venv "$BOT_DIR/.venv"
    "$BOT_DIR/.venv/bin/pip" install --upgrade pip
    "$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt"
fi

cat > "$UNIT" <<EOF
[Unit]
Description=PZ Tambayan Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/run.sh
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo
echo "✅ Installed and started '$SERVICE_NAME'"
echo "   status:  systemctl status $SERVICE_NAME"
echo "   logs:    journalctl -u $SERVICE_NAME -f"
