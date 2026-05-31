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

sudo mkdir -p "$(dirname "$LOG_FILE")"
sudo chown "$SVC_USER:$SVC_USER" "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"; }

# ── Report installed software versions to Supabase ────────────
# Runs in a subshell so sourcing .env doesn't affect the outer set -u scope.
# Failures are always non-fatal.
report_software_versions() {
  (
    ENV_FILE="/etc/chillcheck/.env"
    [ -f "$ENV_FILE" ] || { echo "[report_versions] $ENV_FILE not found, skipping"; exit 0; }
    # shellcheck source=/dev/null
    . "$ENV_FILE"
    [ -n "${SUPABASE_URL:-}" ] && [ -n "${SUPABASE_SERVICE_KEY:-}" ] && [ -n "${DEVICE_ID:-}" ] || {
      echo "[report_versions] Missing env vars, skipping"
      exit 0
    }

    VER_Z2M=$(cat /opt/zigbee2mqtt/package.json 2>/dev/null \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null \
      || echo "unknown")
    VER_MOSQUITTO=$(mosquitto -v 2>&1 | grep -oP '\d+\.\d+\.\d+' | head -1 2>/dev/null || echo "unknown")
    VER_OS=$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}" || echo "unknown")
    VER_PYTHON=$(python3 --version 2>/dev/null | awk '{print $2}' || echo "unknown")
    VER_NODE=$(node --version 2>/dev/null | sed 's/^v//' || echo "unknown")
    VER_SUBSCRIBER="${LATEST:-${CURRENT:-unknown}}"
    PIP="$INSTALL_DIR/venv/bin/pip"
    VER_SENTRY=$(sudo -u "$SVC_USER" "$PIP" show sentry-sdk 2>/dev/null | awk '/^Version/{print $2}')
    VER_SUPABASE=$(sudo -u "$SVC_USER" "$PIP" show supabase 2>/dev/null | awk '/^Version/{print $2}')
    VER_PAHO=$(sudo -u "$SVC_USER" "$PIP" show paho-mqtt 2>/dev/null | awk '/^Version/{print $2}')

    JSON=$(python3 - <<PYEOF
import json, sys
print(json.dumps({
  "subscriber":    "${VER_SUBSCRIBER}",
  "zigbee2mqtt":   "${VER_Z2M}",
  "mosquitto":     "${VER_MOSQUITTO}",
  "os":            "${VER_OS}",
  "python":        "${VER_PYTHON}",
  "node":          "${VER_NODE}",
  "pip": {
    "sentry_sdk":  "${VER_SENTRY:-unknown}",
    "supabase":    "${VER_SUPABASE:-unknown}",
    "paho_mqtt":   "${VER_PAHO:-unknown}"
  },
  "updated_at":    "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}))
PYEOF
)

    curl -sf -X PATCH \
      "${SUPABASE_URL}/rest/v1/devices?id=eq.${DEVICE_ID}" \
      -H "apikey: ${SUPABASE_SERVICE_KEY}" \
      -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}" \
      -H "Content-Type: application/json" \
      -d "{\"software_versions\": ${JSON}}" \
      && log "Software versions reported to Supabase" \
      || log "WARNING: failed to report software versions (non-fatal)"
  )
}

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

# ── Provision system state ────────────────────────────────────
# Runs on every invocation (including "already up to date") so existing hubs
# always catch up on system-level resources without needing a code change.
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

# Sudoers entry for service restarts + Pi reboot (local UI /api/system/restart).
# Written unconditionally so that existing hubs pick up the corrected rules.
SUDOERS_RESTART="/etc/sudoers.d/chillcheck-restart"
printf '%s\n' \
  "$SVC_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart mosquitto" \
  "$SVC_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart zigbee2mqtt" \
  "$SVC_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart chillcheck-subscriber" \
  "$SVC_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart chillcheck-local-ui" \
  "$SVC_USER ALL=(root) NOPASSWD: /usr/sbin/shutdown -r now" | \
  sudo install -m 0440 /dev/stdin "$SUDOERS_RESTART"
log "  Updated $SUDOERS_RESTART"

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

# ── Catch up Python dependencies (always) ─────────────────────
# Runs even on "already up to date" so a venv that's missing a dep declared
# in requirements.txt (e.g. because a previous update wrote the version file
# but the pip step was skipped or failed silently) self-heals on the next
# run. On a true no-op pip is fast — it just confirms each spec is satisfied
# and returns. Failure here is logged but doesn't abort: the health check on
# a real update path catches broken imports, and on the up-to-date path the
# subscriber is already running fine on whatever's installed.
REQS_EARLY="$INSTALL_DIR/subscriber/requirements.txt"
DEPS_CHANGED=0
if [ -f "$REQS_EARLY" ] && [ -x "$INSTALL_DIR/venv/bin/pip" ]; then
  log "Syncing Python dependencies from $REQS_EARLY"
  PIP_LOG="$WORK_DIR/pip-early.log"
  if ! sudo -u "$SVC_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$REQS_EARLY" > "$PIP_LOG" 2>&1; then
    log "WARNING: pip install reported errors — continuing"
    sed 's/^/  pip: /' "$PIP_LOG"
  fi
  if grep -q "^Successfully installed" "$PIP_LOG" 2>/dev/null; then
    DEPS_CHANGED=1
    log "  pip installed new packages"
  fi
fi

if [ "$CURRENT" = "$LATEST" ] && [ "$CURRENT" != "(none)" ]; then
  log "Already up to date."
  if [ "$DEPS_CHANGED" = "1" ]; then
    log "Restarting subscriber to pick up new deps"
    sudo systemctl restart chillcheck-subscriber || true
  fi
  report_software_versions
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

# Self-update: replace the installed updater script so fixes to the
# updater itself propagate via the same Install button on the next run.
# Safe to do after the file swap — bash has already read this file into
# memory and is executing from there.
if [ -f "$WORK_DIR/pi/chillcheck-update.sh" ]; then
  sudo install -m 755 "$WORK_DIR/pi/chillcheck-update.sh" /usr/local/bin/chillcheck-update
fi

# ── Catch up Python dependencies ──────────────────────────────
# Older releases of this script didn't sync requirements.txt, so a release
# that adds a new dep (sentry-sdk in 2026-05) would fail to import and
# fall into the rollback path. Re-running pip install against the venv is
# fast on a no-op (everything already satisfied) and resilient to network
# blips — failure here is logged but doesn't abort the update, since the
# health check downstream will catch any real breakage and roll back.
REQS="$INSTALL_DIR/subscriber/requirements.txt"
if [ -f "$REQS" ] && [ -x "$INSTALL_DIR/venv/bin/pip" ]; then
  log "Syncing Python dependencies from $REQS"
  if ! sudo -u "$SVC_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$REQS"; then
    log "WARNING: pip install reported errors — continuing; health check will catch broken imports"
  fi
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
    report_software_versions
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
