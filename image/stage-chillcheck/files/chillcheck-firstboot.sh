#!/bin/bash
# ============================================================
# ChillCheck — First Boot Setup Script
# /usr/local/bin/chillcheck-firstboot
#
# Runs ONCE on first boot via chillcheck-firstboot.service.
# After completion, disables itself so it never runs again.
#
# Estimated time: 3-8 minutes depending on internet speed.
# ============================================================

set -e

LOG="/var/log/chillcheck/firstboot.log"
INSTALL_DIR="/opt/chillcheck"
CONFIG_DIR="/etc/chillcheck"
FLAG="/etc/chillcheck/.firstboot-complete"

# ── Already run? ──────────────────────────────────────────────
if [ -f "$FLAG" ]; then
  echo "First boot already complete — exiting"
  exit 0
fi

# ── Logging ───────────────────────────────────────────────────
exec > >(tee -a "$LOG") 2>&1
echo ""
echo "=============================================="
echo " ChillCheck First Boot Setup"
echo " $(date)"
echo "=============================================="

# ── Console progress helper ───────────────────────────────────
step() {
  echo ""
  echo "▶ $1"
  # Also write to tty1 so it's visible on HDMI
  echo "ChillCheck: $1" > /dev/tty1 2>/dev/null || true
}

ok() { echo "  ✓ $1"; }
warn() { echo "  ⚠ $1"; }

# ── Wait for network ──────────────────────────────────────────
step "Waiting for network connectivity..."
ATTEMPTS=0
until ping -c1 -W2 8.8.8.8 &>/dev/null; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [ $ATTEMPTS -ge 30 ]; then
    warn "No internet after 60s — some steps may fail"
    break
  fi
  sleep 2
done
ok "Network ready"

# ── System update ─────────────────────────────────────────────
step "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
ok "System updated"

# ── Node.js 20 ───────────────────────────────────────────────
step "Installing Node.js 20..."
if ! command -v node &>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - -qq
  apt-get install -y -qq nodejs
fi
ok "Node.js $(node --version) installed"

# ── Zigbee2MQTT ───────────────────────────────────────────────
step "Installing Zigbee2MQTT..."
if [ ! -f "/opt/zigbee2mqtt/dist/index.js" ]; then
  if [ ! -f "/opt/zigbee2mqtt/package.json" ]; then
    mkdir -p /opt/zigbee2mqtt
    chown chillcheck:chillcheck /opt/zigbee2mqtt
    sudo -u chillcheck git clone --depth 1 \
      https://github.com/Koenkk/zigbee2mqtt.git /opt/zigbee2mqtt
  fi
  # Z2M 2.x uses pnpm via corepack and ships TypeScript source —
  # need full deps (incl. typescript) to run `pnpm run build`.
  corepack enable
  cd /opt/zigbee2mqtt
  sudo -u chillcheck pnpm install --frozen-lockfile
  sudo -u chillcheck pnpm run build
  cd /
fi
ok "Zigbee2MQTT installed"

# ── Detect ZBDongle ───────────────────────────────────────────
step "Detecting ZBDongle-E..."
DONGLE_PORT=""
if [ -e /dev/ttyUSB0 ]; then
  DONGLE_PORT="/dev/ttyUSB0"
elif [ -e /dev/ttyACM0 ]; then
  DONGLE_PORT="/dev/ttyACM0"
else
  DONGLE_PORT="/dev/ttyUSB0"  # default — user can change if needed
  warn "ZBDongle-E not detected — defaulting to /dev/ttyUSB0"
fi
ok "Zigbee adapter: $DONGLE_PORT"

# ── Zigbee2MQTT config ────────────────────────────────────────
step "Writing Zigbee2MQTT configuration..."
mkdir -p /opt/zigbee2mqtt/data
mkdir -p /var/log/chillcheck/zigbee2mqtt
chown chillcheck:chillcheck /var/log/chillcheck/zigbee2mqtt

cat > /opt/zigbee2mqtt/data/configuration.yaml << YAML
homeassistant: false
permit_join: false

mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://127.0.0.1:1883

serial:
  port: $DONGLE_PORT

availability: true

advanced:
  log_level: warn
  log_output:
    - file
  log_directory: /var/log/chillcheck/zigbee2mqtt
  pan_id: GENERATE
  network_key: GENERATE

frontend:
  enabled: false
YAML

chown -R chillcheck:chillcheck /opt/zigbee2mqtt/data
ok "Zigbee2MQTT configured"

# ── Python virtual environment ────────────────────────────────
step "Setting up Python environment..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet \
  paho-mqtt \
  supabase \
  httpx \
  python-dotenv \
  twilio \
  flask \
  flask-cors \
  requests \
  schedule \
  pyserial
ok "Python environment ready"

# ── Fix ownership ─────────────────────────────────────────────
chown -R chillcheck:chillcheck "$INSTALL_DIR"
chown -R chillcheck:chillcheck /var/log/chillcheck

# ── Enable ChillCheck services ────────────────────────────────
step "Enabling services..."
systemctl enable zigbee2mqtt
systemctl enable chillcheck-subscriber
systemctl enable chillcheck-local-ui
ok "Services enabled"

# ── Start local UI immediately ────────────────────────────────
# Start the local UI even before cloud credentials are set
# so Charlie can open chillcheck.local right away
step "Starting local UI..."
systemctl start chillcheck-local-ui
ok "Local UI started — accessible at http://chillcheck.local"

# ── Start Zigbee2MQTT ─────────────────────────────────────────
step "Starting Zigbee2MQTT..."
systemctl start zigbee2mqtt || warn "Zigbee2MQTT failed to start — ZBDongle may not be connected"

# ── Change default password ───────────────────────────────────
step "Setting secure default password..."
# Generate a random password for the 'chillcheck' user
NEW_PASS=$(openssl rand -base64 12 | tr -dc 'a-zA-Z0-9' | head -c 12)
echo "chillcheck:$NEW_PASS" | chpasswd
# Write it to a file on /boot so Charlie can read it from the SD card
echo "ChillCheck default SSH password: $NEW_PASS" > /boot/firmware/chillcheck-password.txt
echo "(Delete this file after noting the password)" >> /boot/firmware/chillcheck-password.txt
ok "Password set — see /boot/firmware/chillcheck-password.txt on the SD card"

# ── Install self-update tooling ───────────────────────────────
step "Installing update tooling..."
if [ -f /opt/chillcheck/chillcheck-update.sh ]; then
  install -m 755 /opt/chillcheck/chillcheck-update.sh /usr/local/bin/chillcheck-update
fi
cat > /etc/sudoers.d/chillcheck-update <<'SUDOERS'
chillcheck ALL=(root) NOPASSWD: /usr/local/bin/chillcheck-update
SUDOERS
chmod 0440 /etc/sudoers.d/chillcheck-update
ok "Update tooling installed"

# ── Mark first boot complete ──────────────────────────────────
touch "$FLAG"

# ── Disable this service ──────────────────────────────────────
systemctl disable chillcheck-firstboot

# ── Final console message ─────────────────────────────────────
echo ""
echo "=============================================="
echo " ChillCheck setup complete!"
echo ""
echo " Open http://chillcheck.local in your browser"
echo " to complete cloud setup and pair sensors."
echo ""
echo " SSH: ssh chillcheck@chillcheck.local"
echo " Password: see SD card /boot/chillcheck-password.txt"
echo "=============================================="
echo ""

# Show on HDMI too
cat > /dev/tty1 2>/dev/null << TTY || true

  ❄  ChillCheck is ready!

  Open http://chillcheck.local in your browser
  to complete setup and pair your sensors.

  Setup log: /var/log/chillcheck/firstboot.log

TTY

exit 0
