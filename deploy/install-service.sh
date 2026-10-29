#!/usr/bin/env bash
# Install the API as a systemd service that starts on boot and restarts on failure.
# Usage: deploy/install-service.sh [port] [device] [torch-extra]
#   e.g. deploy/install-service.sh 8000 cuda cu132
set -euo pipefail

PORT="${1:-8000}"
DEVICE="${2:-cuda}"
TORCH_EXTRA="${3:-cu132}"
HOST="0.0.0.0"
SERVICE=tts-ui-api
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(id -un)"

cd "$PROJECT_DIR"

# build the environment once up front, so the service starts the binary directly
# instead of resolving dependencies through `uv run` on every boot
uv sync --frozen --extra legacy --extra api --extra "$TORCH_EXTRA"

sed -e "s|@USER@|$RUN_USER|g" \
    -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
    -e "s|@DEVICE@|$DEVICE|g" \
    -e "s|@HOST@|$HOST|g" \
    -e "s|@PORT@|$PORT|g" \
    deploy/$SERVICE.service | sudo tee /etc/systemd/system/$SERVICE.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE
sudo systemctl restart $SERVICE
sudo systemctl status $SERVICE --no-pager
