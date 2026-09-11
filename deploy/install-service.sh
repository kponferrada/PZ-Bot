#!/usr/bin/env bash
set -euo pipefail

# Install the PZ Tambayan bot as a systemd service.
# Supports running a second instance (e.g. a test bot) by passing a different
# bot dir and service name.
#
# Usage:
#   sudo ./install-service.sh                                  # live
#   sudo ./install-service.sh /opt/pz-tambayan-bot-test/bot pz-tambayan-bot-test   # test

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOT_DIR="${1:-$REPO_DIR/bot}"
SERVICE_NAME="${2:-pz-tambayan-bot}"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

if [ ! -f "$BOT_DIR/run.sh" ]; then
    echo "ERROR: run.sh not found in $BOT_DIR" >&2
    echo "Usage: $0 [bot-dir] [service-name]   (defaults: $REPO_DIR/bot pz-tambayan-bot)" >&2
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
Description=PZ Tambayan Discord Bot (${SERVICE_NAME})
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
