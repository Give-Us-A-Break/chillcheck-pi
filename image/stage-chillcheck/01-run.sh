#!/bin/bash -e
# ============================================================
# stage-chillcheck/01-run.sh
# Runs inside the pi-gen chroot to configure the image.
# This is NOT the setup script — it prepares the image so
# the setup script runs correctly on first boot.
# ============================================================

# ── Hostname ──────────────────────────────────────────────────
on_chroot << EOF
  hostnamectl set-hostname chillcheck
  sed -i 's/127.0.1.1.*/127.0.1.1\tchillcheck/' /etc/hosts || \
    echo "127.0.1.1\tchillcheck" >> /etc/hosts
EOF

# ── Avahi (mDNS) ─────────────────────────────────────────────
install -m 644 files/avahi-daemon.conf "${ROOTFS_DIR}/etc/avahi/avahi-daemon.conf"

on_chroot << EOF
  systemctl enable avahi-daemon
EOF

# ── Mosquitto (local only) ────────────────────────────────────
install -m 644 files/mosquitto-chillcheck.conf \
  "${ROOTFS_DIR}/etc/mosquitto/conf.d/chillcheck.conf"

on_chroot << EOF
  systemctl enable mosquitto
EOF

# ── ChillCheck directories ────────────────────────────────────
mkdir -p "${ROOTFS_DIR}/opt/chillcheck/subscriber"
mkdir -p "${ROOTFS_DIR}/opt/chillcheck/local_ui"
mkdir -p "${ROOTFS_DIR}/etc/chillcheck"
mkdir -p "${ROOTFS_DIR}/var/log/chillcheck"

# ── Drop in ChillCheck source files ──────────────────────────
# Subscriber
install -m 644 files/subscriber/main.py           "${ROOTFS_DIR}/opt/chillcheck/subscriber/main.py"
install -m 644 files/subscriber/alerting.py       "${ROOTFS_DIR}/opt/chillcheck/subscriber/alerting.py"
install -m 644 files/subscriber/notifications.py  "${ROOTFS_DIR}/opt/chillcheck/subscriber/notifications.py"
install -m 644 files/subscriber/heartbeat.py      "${ROOTFS_DIR}/opt/chillcheck/subscriber/heartbeat.py"
install -m 644 files/subscriber/requirements.txt  "${ROOTFS_DIR}/opt/chillcheck/subscriber/requirements.txt"

# Local UI
install -m 644 files/local_ui/app.py  "${ROOTFS_DIR}/opt/chillcheck/local_ui/app.py"

# Setup script (also available for manual re-run)
install -m 755 files/setup.sh "${ROOTFS_DIR}/usr/local/bin/chillcheck-setup"

# Self-update script (firstboot installs it to /usr/local/bin/chillcheck-update)
install -m 755 files/chillcheck-update.sh "${ROOTFS_DIR}/opt/chillcheck/chillcheck-update.sh"

# .env template
install -m 600 files/env.template "${ROOTFS_DIR}/etc/chillcheck/.env"

# ── First-boot service ────────────────────────────────────────
install -m 644 files/chillcheck-firstboot.service \
  "${ROOTFS_DIR}/etc/systemd/system/chillcheck-firstboot.service"

install -m 755 files/chillcheck-firstboot.sh \
  "${ROOTFS_DIR}/usr/local/bin/chillcheck-firstboot"

on_chroot << EOF
  systemctl enable chillcheck-firstboot
EOF

# ── Placeholder systemd services (enabled after first boot) ──
install -m 644 files/chillcheck-subscriber.service \
  "${ROOTFS_DIR}/etc/systemd/system/chillcheck-subscriber.service"

install -m 644 files/chillcheck-local-ui.service \
  "${ROOTFS_DIR}/etc/systemd/system/chillcheck-local-ui.service"

install -m 644 files/zigbee2mqtt.service \
  "${ROOTFS_DIR}/etc/systemd/system/zigbee2mqtt.service"

# Don't enable them yet — first-boot script does that after setup

# ── UFW firewall rules ────────────────────────────────────────
on_chroot << EOF
  ufw --force reset
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow ssh
  ufw allow 80/tcp
  ufw allow 5353/udp
  ufw --force enable
EOF

# ── Splash / MOTD ─────────────────────────────────────────────
install -m 644 files/motd "${ROOTFS_DIR}/etc/motd"
# Disable default MOTD scripts
rm -f "${ROOTFS_DIR}/etc/update-motd.d/"*

# ── Boot console message ──────────────────────────────────────
# Shows progress on HDMI/serial during first boot
install -m 644 files/chillcheck-boot-msg.service \
  "${ROOTFS_DIR}/etc/systemd/system/chillcheck-boot-msg.service"

on_chroot << EOF
  systemctl enable chillcheck-boot-msg
EOF

echo "stage-chillcheck: 01-run.sh complete"
