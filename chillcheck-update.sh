#!/bin/bash
# ============================================================
# /usr/local/bin/chillcheck-update
# Pulls latest Pi code from the public chillcheck-pi mirror,
# replaces /opt/chillcheck/{subscriber,local_ui}, restarts
# services, and rolls back if a service fails to come up.
#
# Idempotent. Safe to re-run. Exit codes:
#   0  already up to date OR successfully updated
#   1  fetch / network failure
#   2  apply / restart failure (rolled back)
# ============================================================

set -u
set -o pipefail

REPO_URL="https://github.com/Give-Us-A-Break/chillcheck-pi.git"
INSTALL_DIR="/opt/chillcheck"
BACKUP_DIR="/opt/chillcheck/.previous"
VERSION_FILE="/etc/chillcheck/version"
LOG_FILE="/var/log/chillcheck/update.log"
WORK_DIR="$(mktemp -d /tmp/chillcheck-update.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"; }

CURRENT="(none)"
if [ -f "$VERSION_FILE" ]; then
  CURRENT=$(cat "$VERSION_FILE")
fi

log "Update started. Current version: $CURRENT"

# ── Fetch latest ──────────────────────────────────────────────
if ! git clone --depth 1 --quiet "$REPO_URL" "$WORK_DIR/pi"; then
  log "ERROR: clone failed."
  exit 1
fi

LATEST="(unknown)"
if [ -f "$WORK_DIR/pi/VERSION" ]; then
  LATEST=$(cat "$WORK_DIR/pi/VERSION")
fi

log "Latest version: $LATEST"

if [ "$CURRENT" = "$LATEST" ] && [ "$CURRENT" != "(none)" ]; then
  log "Already up to date."
  exit 0
fi

# ── Back up current installation ──────────────────────────────
log "Backing up current installation to $BACKUP_DIR"
sudo rm -rf "$BACKUP_DIR"
sudo mkdir -p "$BACKUP_DIR"
[ -d "$INSTALL_DIR/subscriber" ] && sudo cp -a "$INSTALL_DIR/subscriber" "$BACKUP_DIR/"
[ -d "$INSTALL_DIR/local_ui"   ] && sudo cp -a "$INSTALL_DIR/local_ui"   "$BACKUP_DIR/"

# ── Stop services ─────────────────────────────────────────────
log "Stopping services"
sudo systemctl stop chillcheck-subscriber chillcheck-local-ui

# ── Swap files ────────────────────────────────────────────────
# Strip CRLF from any Windows-edited files (idempotent for unix files).
find "$WORK_DIR/pi" -type f \( -name '*.py' -o -name '*.sh' -o -name '*.service' \
                              -o -name '*.conf' -o -name '*.yaml' \) \
  -exec sed -i 's/\r$//' {} +

if [ -d "$WORK_DIR/pi/subscriber" ]; then
  sudo cp -a "$WORK_DIR/pi/subscriber/." "$INSTALL_DIR/subscriber/"
fi
if [ -d "$WORK_DIR/pi/local_ui" ]; then
  sudo cp -a "$WORK_DIR/pi/local_ui/." "$INSTALL_DIR/local_ui/"
fi
sudo chown -R chillcheck:chillcheck "$INSTALL_DIR/subscriber" "$INSTALL_DIR/local_ui"

# ── Start services + health check ─────────────────────────────
log "Starting services"
sudo systemctl start chillcheck-local-ui chillcheck-subscriber

healthy() {
  systemctl is-active --quiet chillcheck-subscriber && \
  systemctl is-active --quiet chillcheck-local-ui
}

# Give services up to 30s to come up
for i in {1..15}; do
  sleep 2
  if healthy; then
    sudo mkdir -p "$(dirname "$VERSION_FILE")"
    echo "$LATEST" | sudo tee "$VERSION_FILE" > /dev/null
    log "Update applied successfully: $CURRENT -> $LATEST"
    exit 0
  fi
done

# ── Rollback ──────────────────────────────────────────────────
log "ERROR: services unhealthy after 30s, rolling back"
sudo systemctl stop chillcheck-subscriber chillcheck-local-ui
[ -d "$BACKUP_DIR/subscriber" ] && sudo cp -a "$BACKUP_DIR/subscriber/." "$INSTALL_DIR/subscriber/"
[ -d "$BACKUP_DIR/local_ui"   ] && sudo cp -a "$BACKUP_DIR/local_ui/."   "$INSTALL_DIR/local_ui/"
sudo chown -R chillcheck:chillcheck "$INSTALL_DIR/subscriber" "$INSTALL_DIR/local_ui"
sudo systemctl start chillcheck-local-ui chillcheck-subscriber
log "Rolled back to $CURRENT"
exit 2
