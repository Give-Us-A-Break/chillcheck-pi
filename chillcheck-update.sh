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

# We are usually launched as a child of chillcheck-local-ui.service.
# That service has the default KillMode=control-group, so when we later
# run `systemctl stop chillcheck-local-ui` to swap files, systemd kills
# our entire cgroup — including this script. Re-exec under systemd-run
# so the rest of the work lives in its own transient scope and survives.
if [ "${CHILLCHECK_UPDATE_DETACHED:-}" != "1" ] && command -v systemd-run >/dev/null 2>&1; then
  exec systemd-run --quiet --collect \
    --unit="chillcheck-update-$$" \
    --setenv=CHILLCHECK_UPDATE_DETACHED=1 \
    "$0" "$@"
fi

set -u
set -o pipefail

REPO_URL="https://github.com/Give-Us-A-Break/chillcheck-pi.git"
INSTALL_DIR="/opt/chillcheck"
BACKUP_DIR="/opt/chillcheck/.previous"
DATA_DIR="/var/lib/chillcheck"
VERSION_FILE="/etc/chillcheck/version"
LOG_FILE="/var/log/chillcheck/update.log"
# Service user matches the username assumed elsewhere in this script
# (the existing chown -R below uses chillcheck:chillcheck). Hardcoded
# rather than derived from $SUDO_USER because systemd-run loses that
# context when we re-exec at the top of the script.
SVC_USER="chillcheck"
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

# ── Provision system state ────────────────────────────────────
# Idempotent. Catches up any system-level resources newer releases
# expect to find. setup.sh creates these on a fresh install; the updater
# replays them here so an existing hub can pick up changes without a
# re-flash. Always safe to run.
log "Provisioning system state (data dir, sudoers, log dir)"

# Persistent application data (Epic 10 buffer.db lives here)
if [ ! -d "$DATA_DIR" ]; then
  sudo mkdir -p "$DATA_DIR"
  log "  Created $DATA_DIR"
fi
sudo chown "$SVC_USER:$SVC_USER" "$DATA_DIR"

# Log directory (some installs created this lazily)
if [ ! -d "/var/log/chillcheck" ]; then
  sudo mkdir -p /var/log/chillcheck
  sudo chown "$SVC_USER:$SVC_USER" /var/log/chillcheck
fi

# Sudoers entry for journalctl (Logs tab needs this; only setup.sh used
# to install it). install -m 0440 is atomic via temp + rename so a partial
# write can't leave a syntactically broken sudoers file on disk.
SUDOERS_JOURNAL="/etc/sudoers.d/chillcheck-journalctl"
if ! sudo test -f "$SUDOERS_JOURNAL"; then
  echo "$SVC_USER ALL=(root) NOPASSWD: /usr/bin/journalctl" | \
    sudo install -m 0440 /dev/stdin "$SUDOERS_JOURNAL"
  log "  Installed $SUDOERS_JOURNAL"
fi

# 4G failover dispatcher (Epic 10 slice 4). Installs/updates idempotently so
# existing hubs pick it up automatically without a re-flash.
DISPATCHER="/etc/NetworkManager/dispatcher.d/10-chillcheck-4g-failover"
if ! sudo test -f "$DISPATCHER" 2>/dev/null; then
  sudo tee "$DISPATCHER" > /dev/null <<'DISPEOF'
#!/bin/bash
# NetworkManager dispatcher — ChillCheck 4G failover
# Sets route metric 700 on any USB-ethernet adapter so it acts as a
# secondary route. Primary ethernet (eth0, metric ~100) is always preferred.
IFACE="$1"
ACTION="$2"

[ "$ACTION" = "up" ] || exit 0

case "$IFACE" in eth0|wlan0|wlan1|lo) exit 0 ;; esac

NET_TYPE=$(cat "/sys/class/net/${IFACE}/type" 2>/dev/null)
[ "$NET_TYPE" = "1" ] || exit 0

DEVPATH=$(readlink -f "/sys/class/net/${IFACE}/device" 2>/dev/null || echo "")
[[ "$DEVPATH" == */usb* ]] || exit 0

ip route change default dev "$IFACE" metric 700 2>/dev/null \
  || ip route add    default dev "$IFACE" metric 700 2>/dev/null \
  || true

if [ -n "${CONNECTION_ID:-}" ]; then
  nmcli connection modify id "$CONNECTION_ID" ipv4.route-metric 700 2>/dev/null || true
fi

logger -t chillcheck "4G failover: ${IFACE} (${CONNECTION_ID:-unknown}) configured as secondary route (metric 700)"
DISPEOF
  sudo chmod 755 "$DISPATCHER"
  log "  Installed 4G failover dispatcher"
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

# Self-update: replace the installed updater script so fixes to the
# updater itself propagate via the same Install button on the next run.
# Safe to do after the file swap — bash has already read this file into
# memory and is executing from there.
if [ -f "$WORK_DIR/pi/chillcheck-update.sh" ]; then
  sudo install -m 755 "$WORK_DIR/pi/chillcheck-update.sh" /usr/local/bin/chillcheck-update
fi

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
