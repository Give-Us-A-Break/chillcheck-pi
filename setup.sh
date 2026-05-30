#!/bin/bash
# ============================================================
# ChillCheck — Pi 4 Setup Script
# Run this on a fresh Raspberry Pi OS Lite (64-bit) install
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/your-repo/chillcheck/main/setup.sh | bash
#   or: chmod +x setup.sh && ./setup.sh
#
# What this does:
#   1. Sets hostname to chillcheck
#   2. Installs and configures Avahi (mDNS — chillcheck.local)
#   3. Installs Mosquitto (MQTT broker)
#   4. Installs Node.js and Zigbee2MQTT
#   5. Installs Python 3 and pip dependencies
#   6. Installs Flask (local UI server)
#   7. Creates config directory and .env template
#   8. Registers all services with systemd
#   9. Prints summary and next steps
# ============================================================

set -e  # Exit on any error

# ── Colours ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log()     { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; exit 1; }
section() { echo -e "\n${BLUE}${BOLD}── $1 ──${NC}"; }

# ── Check we're running on a Pi ──────────────────────────────
if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
  warn "This doesn't look like a Raspberry Pi. Continuing anyway..."
fi

# ── Check running as non-root with sudo ──────────────────────
if [ "$EUID" -eq 0 ]; then
  error "Don't run as root. Run as the 'pi' user with sudo privileges."
fi

INSTALL_DIR="/opt/chillcheck"
CONFIG_DIR="/etc/chillcheck"
LOG_DIR="/var/log/chillcheck"
DATA_DIR="/var/lib/chillcheck"
USER=$(whoami)

echo ""
echo -e "${BLUE}${BOLD}"
echo "   ___  _     _ _ _  ___ _               _    "
echo "  / __|| |_  (_) | ||  _| |_  ___ __ _ _| |__ "
echo " | (__ | ' \ | | | || |_| ' \/ -_) _| '_| / / "
echo "  \___||_||_||_|_|_||___|_||_\___\__|_| |_\_\ "
echo ""
echo -e "${NC}  Temperature Monitoring — Pi 4 Setup"
echo "  Running as: $USER"
echo "  Install dir: $INSTALL_DIR"
echo ""


# ════════════════════════════════════════════════════════════
# PHASE 1 — SYSTEM PREP
# ════════════════════════════════════════════════════════════

section "Phase 1 — System Prep"

log "Updating package lists..."
sudo apt-get update -qq

log "Upgrading installed packages..."
sudo apt-get upgrade -y -qq

log "Installing base dependencies..."
sudo apt-get install -y -qq \
  curl wget git vim \
  python3 python3-pip python3-venv \
  build-essential \
  avahi-daemon avahi-utils \
  dbus \
  jq \
  ufw \
  unzip

log "Creating ChillCheck directories..."
sudo mkdir -p "$INSTALL_DIR"
sudo mkdir -p "$CONFIG_DIR"
sudo mkdir -p "$LOG_DIR"
sudo mkdir -p "$DATA_DIR"
sudo chown -R "$USER:$USER" "$INSTALL_DIR"
sudo chown -R "$USER:$USER" "$LOG_DIR"
sudo chown -R "$USER:$USER" "$DATA_DIR"


# ════════════════════════════════════════════════════════════
# PHASE 2 — HOSTNAME & mDNS
# ════════════════════════════════════════════════════════════

section "Phase 2 — Hostname & mDNS (chillcheck.local)"

log "Setting hostname to 'chillcheck'..."
sudo hostnamectl set-hostname chillcheck

# Update /etc/hosts
if ! grep -q "chillcheck" /etc/hosts; then
  sudo sed -i "s/127.0.1.1.*/127.0.1.1\tchillcheck/" /etc/hosts
fi

log "Configuring Avahi for mDNS..."
sudo tee /etc/avahi/avahi-daemon.conf > /dev/null <<'EOF'
[server]
host-name=chillcheck
domain-name=local
use-ipv4=yes
use-ipv6=no
allow-interfaces=eth0,wlan0
ratelimit-interval-usec=1000000
ratelimit-burst=1000

[wide-area]
enable-wide-area=no

[publish]
publish-addresses=yes
publish-hinfo=yes
publish-workstation=yes
publish-domain=yes

[reflector]
enable-reflector=no

[rlimits]
EOF

sudo systemctl enable avahi-daemon
sudo systemctl restart avahi-daemon
log "mDNS configured — device will be reachable at http://chillcheck.local"


# ════════════════════════════════════════════════════════════
# PHASE 3 — MOSQUITTO (MQTT BROKER)
# ════════════════════════════════════════════════════════════

section "Phase 3 — Mosquitto MQTT Broker"

log "Installing Mosquitto..."
sudo apt-get install -y -qq mosquitto mosquitto-clients

log "Configuring Mosquitto..."
sudo tee /etc/mosquitto/conf.d/chillcheck.conf > /dev/null <<'EOF'
# ChillCheck Mosquitto config
# Listens on localhost only — Zigbee2MQTT and subscriber
# are on the same Pi, no external MQTT needed.
# Logging is left to Debian's default /etc/mosquitto/mosquitto.conf;
# duplicating log_dest here causes mosquitto to refuse to start.

listener 1883 127.0.0.1
allow_anonymous true
EOF

sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
log "Mosquitto running on 127.0.0.1:1883"


# ════════════════════════════════════════════════════════════
# PHASE 4 — NODE.JS & ZIGBEE2MQTT
# ════════════════════════════════════════════════════════════

section "Phase 4 — Node.js & Zigbee2MQTT"

log "Installing Node.js 20 LTS..."
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y -qq nodejs
node_version=$(node --version)
log "Node.js $node_version installed"

log "Installing Zigbee2MQTT..."
sudo mkdir -p /opt/zigbee2mqtt
sudo chown -R "$USER:$USER" /opt/zigbee2mqtt
if [ ! -f /opt/zigbee2mqtt/dist/index.js ]; then
  if [ ! -f /opt/zigbee2mqtt/package.json ]; then
    git clone --depth 1 https://github.com/Koenkk/zigbee2mqtt.git /opt/zigbee2mqtt
  fi
  # Z2M >= 2.x switched to pnpm; corepack ships with Node 20 and reads
  # the "packageManager" field in package.json. The full (non --prod)
  # install is required so devDependencies (typescript) are present
  # for the initial `pnpm run build` step.
  sudo corepack enable
  cd /opt/zigbee2mqtt
  pnpm install --frozen-lockfile
  pnpm run build
  cd -
else
  log "Zigbee2MQTT already built — skipping"
fi

log "Detecting ZBDongle-E..."
# ZBDongle-E shows up as /dev/ttyUSB0 or /dev/ttyACM0
DONGLE_PORT=""
if [ -e /dev/ttyUSB0 ]; then
  DONGLE_PORT="/dev/ttyUSB0"
elif [ -e /dev/ttyACM0 ]; then
  DONGLE_PORT="/dev/ttyACM0"
else
  warn "ZBDongle-E not detected. Defaulting to /dev/ttyUSB0 — plug it in before starting."
  DONGLE_PORT="/dev/ttyUSB0"
fi
log "Zigbee adapter: $DONGLE_PORT"

log "Writing Zigbee2MQTT configuration..."
mkdir -p /opt/zigbee2mqtt/data
tee /opt/zigbee2mqtt/data/configuration.yaml > /dev/null <<EOF
# Zigbee2MQTT — ChillCheck configuration
homeassistant: false
permit_join: false

mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://127.0.0.1:1883

serial:
  port: $DONGLE_PORT

# Publish device availability (used for offline detection)
availability: true

# Friendly names will be set via the local UI
# Devices appear as their Zigbee ID until named

advanced:
  log_level: warn
  log_output:
    - file
  log_directory: /var/log/chillcheck/zigbee2mqtt
  pan_id: GENERATE
  network_key: GENERATE

frontend:
  enabled: false   # We use ChillCheck's own local UI

device_options:
  retain: false
EOF

sudo mkdir -p /var/log/chillcheck/zigbee2mqtt
sudo chown -R "$USER:$USER" /var/log/chillcheck/zigbee2mqtt

log "Zigbee2MQTT configured"


# ════════════════════════════════════════════════════════════
# PHASE 5 — PYTHON ENVIRONMENT
# ════════════════════════════════════════════════════════════

section "Phase 5 — Python Environment"

log "Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
source "$INSTALL_DIR/venv/bin/activate"

log "Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet \
  paho-mqtt \
  supabase \
  httpx \
  python-dotenv \
  flask \
  flask-cors \
  requests \
  schedule \
  pyserial \
  'sentry-sdk>=2.0,<3.0'

deactivate
log "Python dependencies installed in $INSTALL_DIR/venv"


# ════════════════════════════════════════════════════════════
# PHASE 6 — CHILLCHECK CONFIG FILE
# ════════════════════════════════════════════════════════════

section "Phase 6 — ChillCheck Configuration"

log "Creating config template..."
sudo tee "$CONFIG_DIR/.env" > /dev/null <<'EOF'
# ============================================================
# ChillCheck — Environment Configuration
# Fill these in after running setup, then restart services.
# ============================================================

# ── Supabase ─────────────────────────────────────────────────
# Get these from your Supabase project settings → API
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key-here

# ── Pairing ──────────────────────────────────────────────────
# Set automatically when Pi is linked via pairing code
ORGANISATION_ID=
SITE_ID=
DEVICE_ID=

# ── MQTT ─────────────────────────────────────────────────────
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_TOPIC_PREFIX=zigbee2mqtt

# ── Alerting ─────────────────────────────────────────────────
# Set automatically when Pi is linked via pairing code.
# The Pi never holds Resend or Twilio credentials — all
# notification sending is proxied through ChillCheck's cloud API.
NOTIFY_SECRET=

# ── Heartbeat ────────────────────────────────────────────────
# URL pinged every 5 minutes to confirm Pi is alive
# Get this from Uptime Robot (free tier)
HEARTBEAT_URL=https://heartbeat.uptimerobot.com/your-monitor-key

# ── Local UI ─────────────────────────────────────────────────
LOCAL_UI_PORT=80

# ── Logging ──────────────────────────────────────────────────
LOG_LEVEL=INFO
EOF

sudo chown "$USER:$USER" "$CONFIG_DIR/.env"
sudo chmod 600 "$CONFIG_DIR/.env"
log "Config written to $CONFIG_DIR/.env — fill in credentials before starting"


# ════════════════════════════════════════════════════════════
# PHASE 7 — SYSTEMD SERVICES
# ════════════════════════════════════════════════════════════

section "Phase 7 — Systemd Services"

# ── Zigbee2MQTT service ──────────────────────────────────────
log "Creating Zigbee2MQTT systemd service..."
sudo tee /etc/systemd/system/zigbee2mqtt.service > /dev/null <<EOF
[Unit]
Description=ChillCheck — Zigbee2MQTT
Documentation=https://www.zigbee2mqtt.io
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
Type=simple
User=$USER
WorkingDirectory=/opt/zigbee2mqtt
ExecStart=/usr/bin/node /opt/zigbee2mqtt/index.js
Restart=on-failure
RestartSec=10s
StandardOutput=append:/var/log/chillcheck/zigbee2mqtt/service.log
StandardError=append:/var/log/chillcheck/zigbee2mqtt/service.log

[Install]
WantedBy=multi-user.target
EOF

# ── ChillCheck subscriber service ───────────────────────────
log "Creating subscriber systemd service..."
sudo tee /etc/systemd/system/chillcheck-subscriber.service > /dev/null <<EOF
[Unit]
Description=ChillCheck — MQTT Subscriber & Alerting
After=network.target mosquitto.service zigbee2mqtt.service
Requires=mosquitto.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONFIG_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/subscriber/main.py
Restart=on-failure
RestartSec=15s
StandardOutput=append:$LOG_DIR/subscriber.log
StandardError=append:$LOG_DIR/subscriber.log

[Install]
WantedBy=multi-user.target
EOF

# ── ChillCheck local UI service ──────────────────────────────
log "Creating local UI systemd service..."
sudo tee /etc/systemd/system/chillcheck-local-ui.service > /dev/null <<EOF
[Unit]
Description=ChillCheck — Local Setup UI
After=network.target
Wants=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONFIG_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/local_ui/app.py
# Allow non-root user to bind to port 80
AmbientCapabilities=CAP_NET_BIND_SERVICE
# DO NOT add CapabilityBoundingSet here. It would be inherited by every
# child of this service, including the sudo we spawn from /api/system/update/run.
# A restricted bounding set strips CAP_SETUID/SETGID from setuid binaries,
# so sudo silently fails to elevate — no journald entry, no script output,
# the UI just spins on "Updating…". AmbientCapabilities alone is enough
# to grant the non-root user the bind-to-low-port capability.
Restart=on-failure
RestartSec=10s
StandardOutput=append:$LOG_DIR/local_ui.log
StandardError=append:$LOG_DIR/local_ui.log

[Install]
WantedBy=multi-user.target
EOF

log "Reloading systemd..."
sudo systemctl daemon-reload

# Enable but don't start yet — needs .env filled in first
sudo systemctl enable zigbee2mqtt
sudo systemctl enable chillcheck-subscriber
sudo systemctl enable chillcheck-local-ui

log "Services registered (will start after credentials are configured)"


# ════════════════════════════════════════════════════════════
# PHASE 8 — FIREWALL
# ════════════════════════════════════════════════════════════

section "Phase 8 — Firewall"

log "Configuring UFW firewall..."
sudo ufw --force reset
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh           # SSH access
sudo ufw allow 80/tcp        # Local UI (HTTP)
sudo ufw allow 5353/udp      # mDNS (Avahi)
# Mosquitto stays on localhost only — no external access needed
sudo ufw --force enable
log "Firewall configured"


# ════════════════════════════════════════════════════════════
# PHASE 9 — PLACEHOLDER APP FILES
# Creates the directory structure for Phase 3 (subscriber)
# and Phase 4 (local UI) code to be dropped into
# ════════════════════════════════════════════════════════════

section "Phase 9 — App Directory Structure"

log "Creating app directory structure..."

mkdir -p "$INSTALL_DIR/subscriber"
mkdir -p "$INSTALL_DIR/local_ui/templates"
mkdir -p "$INSTALL_DIR/local_ui/static"

# Placeholder subscriber main.py
tee "$INSTALL_DIR/subscriber/main.py" > /dev/null <<'EOF'
# ChillCheck Subscriber — placeholder
# Replace with full implementation in Phase 3
print("ChillCheck subscriber starting...")
print("Add subscriber/main.py content from Phase 3")
EOF

# Placeholder local UI app.py
tee "$INSTALL_DIR/local_ui/app.py" > /dev/null <<'EOF'
# ChillCheck Local UI — placeholder
# Replace with full implementation in Phase 4
from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "<h1>ChillCheck Local UI</h1><p>Setup in progress. Add local_ui/app.py from Phase 4.</p>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
EOF

log "Directory structure created at $INSTALL_DIR"


# ════════════════════════════════════════════════════════════
# PHASE 10 — UPDATE MECHANISM
# Installs chillcheck-update so the Pi can self-update from the
# public chillcheck-pi mirror, and grants the chillcheck user
# permission to invoke it without an interactive password prompt.
# ════════════════════════════════════════════════════════════

section "Phase 10 — Update Mechanism"

# Install the update script if it was shipped alongside setup.sh
SETUP_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SETUP_DIR/chillcheck-update.sh" ]; then
  sudo install -m 755 "$SETUP_DIR/chillcheck-update.sh" /usr/local/bin/chillcheck-update
  # Strip CRLF in case the file came from a Windows checkout. A shebang line
  # ending in \r causes the kernel to look for an interpreter literally named
  # "/bin/bash\r" and fail with a confusing "No such file or directory".
  sudo sed -i 's/\r$//' /usr/local/bin/chillcheck-update
  log "Installed /usr/local/bin/chillcheck-update"
else
  warn "chillcheck-update.sh not found next to setup.sh — skipping update script install"
fi

# Sudoers rule so the local UI service (running as 'chillcheck')
# can invoke the update script without a password prompt.
echo "$USER ALL=(root) NOPASSWD: /usr/local/bin/chillcheck-update" | sudo tee /etc/sudoers.d/chillcheck-update > /dev/null
sudo chmod 0440 /etc/sudoers.d/chillcheck-update
log "Granted $USER passwordless sudo for /usr/local/bin/chillcheck-update"

# Sudoers rule for journalctl so the local UI's Logs tab can read service
# journals. Scoped to /usr/bin/journalctl only; the route whitelists
# unit names before invoking the command.
echo "$USER ALL=(root) NOPASSWD: /usr/bin/journalctl" | sudo tee /etc/sudoers.d/chillcheck-journalctl > /dev/null
sudo chmod 0440 /etc/sudoers.d/chillcheck-journalctl
log "Granted $USER passwordless sudo for /usr/bin/journalctl"

# Sudoers rule for service restarts and Pi reboot from the local UI.
# The /api/system/restart route invokes these; without this rule the
# chillcheck service user gets a sudo password prompt and the call fails.
cat <<SUDOERS | sudo tee /etc/sudoers.d/chillcheck-restart > /dev/null
$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart mosquitto
$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart zigbee2mqtt
$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart chillcheck-subscriber
$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart chillcheck-local-ui
$USER ALL=(root) NOPASSWD: /usr/sbin/shutdown -r now
SUDOERS
sudo chmod 0440 /etc/sudoers.d/chillcheck-restart
log "Granted $USER passwordless sudo for service restarts and Pi reboot"


# ════════════════════════════════════════════════════════════
# PHASE 11 — 4G FAILOVER (NetworkManager dispatcher)
# Configures automatic failover to a 4G USB dongle when the
# primary ethernet drops. Activates automatically when any
# HiLink-mode dongle (e.g. Huawei E3372) is plugged in — no
# manual steps required.
# ════════════════════════════════════════════════════════════

section "Phase 11 — 4G Failover (NetworkManager)"

# NetworkManager is installed and active on Pi OS Bookworm by default.
# Install it defensively in case this is an older or minimal image.
if ! command -v nmcli &>/dev/null; then
  log "Installing NetworkManager..."
  sudo apt-get install -y -qq network-manager
fi

# Dispatcher script: whenever a USB-ethernet adapter comes up (Huawei E3372
# in HiLink mode presents as a CDC Ethernet/RNDIS device on usb0, eth1, or
# enxXXX), assign it route metric 700. The primary ethernet (eth0) defaults
# to ~100, so the Pi stays on broadband while it's up and falls over to 4G
# only when it drops. The metric is also persisted to the NM connection
# profile so it survives reconnects.
log "Installing 4G failover dispatcher..."
sudo tee /etc/NetworkManager/dispatcher.d/10-chillcheck-4g-failover > /dev/null <<'DISPEOF'
#!/bin/bash
# NetworkManager dispatcher — ChillCheck 4G failover
# Sets route metric 700 on any USB-ethernet adapter so it acts as a
# secondary route. Primary ethernet (eth0, metric ~100) is always preferred.
IFACE="$1"
ACTION="$2"

[ "$ACTION" = "up" ] || exit 0

# Skip primary wired and wireless interfaces
case "$IFACE" in eth0|wlan0|wlan1|lo) exit 0 ;; esac

# Only act on Ethernet-type interfaces (ARPHRD_ETHER = 1); skips WiFi, PPP
NET_TYPE=$(cat "/sys/class/net/${IFACE}/type" 2>/dev/null)
[ "$NET_TYPE" = "1" ] || exit 0

# Only act if the device is USB-backed (HiLink modem, RNDIS, CDC Ethernet).
# The device path symlink passes through /sys/bus/usb/ for USB devices.
DEVPATH=$(readlink -f "/sys/class/net/${IFACE}/device" 2>/dev/null || echo "")
[[ "$DEVPATH" == */usb* ]] || exit 0

# Apply high metric immediately so traffic continues via eth0 while it's up
ip route change default dev "$IFACE" metric 700 2>/dev/null \
  || ip route add    default dev "$IFACE" metric 700 2>/dev/null \
  || true

# Persist in the NM connection profile so the metric survives reconnects.
# CONNECTION_ID is set by NetworkManager for each dispatcher invocation.
if [ -n "${CONNECTION_ID:-}" ]; then
  nmcli connection modify id "$CONNECTION_ID" ipv4.route-metric 700 2>/dev/null || true
fi

logger -t chillcheck "4G failover: ${IFACE} (${CONNECTION_ID:-unknown}) configured as secondary route (metric 700)"
DISPEOF

sudo chmod 755 /etc/NetworkManager/dispatcher.d/10-chillcheck-4g-failover
log "4G failover dispatcher installed — plug in a HiLink USB dongle to activate automatically"


# ════════════════════════════════════════════════════════════
# DONE
# ════════════════════════════════════════════════════════════

section "Setup Complete"

# Get current IP
IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}${BOLD}ChillCheck Pi 4 setup complete!${NC}"
echo ""
echo -e "${BOLD}Device info:${NC}"
echo "  Hostname:    chillcheck"
echo "  Local URL:   http://chillcheck.local"
echo "  IP address:  http://$IP"
echo ""
echo -e "${BOLD}Next steps:${NC}"
echo ""
echo -e "  ${YELLOW}1.${NC} Fill in credentials:"
echo "     sudo nano $CONFIG_DIR/.env"
echo ""
echo -e "  ${YELLOW}2.${NC} Plug in the ZBDongle-E (if not already)"
echo "     Check it's detected: ls /dev/ttyUSB*"
echo ""
echo -e "  ${YELLOW}3.${NC} Deploy Phase 3 subscriber code:"
echo "     Copy subscriber/main.py to $INSTALL_DIR/subscriber/"
echo ""
echo -e "  ${YELLOW}4.${NC} Deploy Phase 4 local UI code:"
echo "     Copy local_ui/app.py to $INSTALL_DIR/local_ui/"
echo ""
echo -e "  ${YELLOW}5.${NC} Start all services:"
echo "     sudo systemctl start zigbee2mqtt"
echo "     sudo systemctl start chillcheck-subscriber"
echo "     sudo systemctl start chillcheck-local-ui"
echo ""
echo -e "  ${YELLOW}6.${NC} Check service status:"
echo "     sudo systemctl status chillcheck-subscriber"
echo "     sudo journalctl -u chillcheck-subscriber -f"
echo ""
echo -e "  ${YELLOW}7.${NC} Open the local UI in your browser:"
echo "     http://chillcheck.local"
echo ""
echo -e "${BLUE}Logs are at: $LOG_DIR${NC}"
echo ""
