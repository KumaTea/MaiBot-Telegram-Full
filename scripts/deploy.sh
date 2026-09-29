#!/usr/bin/env bash
# Deploy the plugin into a MaiBot docker container over SSH.
#
#   scripts/deploy.sh [--restart]
#
# Environment overrides:
#   DEPLOY_HOST       ssh destination            (default kuma@10.3.3.5)
#   DEPLOY_KEY        ssh private key            (default /home/kuma/certs/ssh/kuma.key)
#   DEPLOY_CONTAINER  MaiBot container name      (default maim-bot-core)
#   DEPLOY_DIR        plugin dir in container    (default /MaiMBot/plugins/kumatea_telegram-full)
#
# config.toml on the server is never touched. MaiBot hot-reloads changed plugin sources; use
# --restart after the first deploy or when dependencies change (they are installed at startup).
set -euo pipefail

HOST="${DEPLOY_HOST:-kuma@10.3.3.5}"
KEY="${DEPLOY_KEY:-/home/kuma/certs/ssh/kuma.key}"
CONTAINER="${DEPLOY_CONTAINER:-maim-bot-core}"
DEST="${DEPLOY_DIR:-/MaiMBot/plugins/kumatea_telegram-full}"

cd "$(dirname "$0")/.."

tar --exclude='__pycache__' --exclude='*.pyc' -czf - plugin.py _manifest.json README.md tg_full \
  | ssh -i "$KEY" "$HOST" "docker exec -i $CONTAINER sh -c 'mkdir -p $DEST && rm -rf $DEST/tg_full $DEST/plugin.py && tar -xzf - -C $DEST'"
echo "Deployed to $CONTAINER:$DEST"

if [[ "${1:-}" == "--restart" ]]; then
  ssh -i "$KEY" "$HOST" "docker restart $CONTAINER" >/dev/null
  echo "Restarted $CONTAINER"
fi
