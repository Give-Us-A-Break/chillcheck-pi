# ChillCheck OS Image — Build & Flash Guide

## What this produces

A custom Raspberry Pi OS image that:
- Auto-configures everything on first boot (3–8 mins)
- Serves `http://chillcheck.local` immediately after
- Requires zero terminal commands from Charlie

---

## Prerequisites — Build Machine

You need a **Linux machine** to build the image (not the Pi itself).
Ubuntu 22.04+ or Debian Bookworm recommended.

```bash
# Install pi-gen dependencies
sudo apt-get install -y \
  coreutils quilt parted qemu-user-static debootstrap \
  zerofree zip dosfstools libarchive-tools libcap2-bin \
  grep rsync xz-utils file git curl bc gpg pigz xxd \
  arch-test binfmt-support
```

---

## Build Steps

```bash
# 1. Clone pi-gen
git clone https://github.com/RPi-Distro/pi-gen.git
cd pi-gen

# 2. Copy ChillCheck stage and config
cp -r /path/to/chillcheck-image/stage-chillcheck ./stage-chillcheck
cp    /path/to/chillcheck-image/config            ./config

# 3. Copy source files into the stage
cp /path/to/chillcheck/pi/subscriber/*.py         stage-chillcheck/files/subscriber/
cp /path/to/chillcheck/pi/subscriber/requirements.txt stage-chillcheck/files/subscriber/
cp /path/to/chillcheck/pi/local_ui/app.py         stage-chillcheck/files/local_ui/
cp /path/to/chillcheck/pi/setup.sh                stage-chillcheck/files/setup.sh

# 4. Mark stage-chillcheck as active (create SKIP files for unused stages)
touch stage-chillcheck/SKIP_IMAGES  # Don't create image at this stage
# pi-gen creates the final image after all stages

# 5. Build (takes 20-40 mins)
sudo ./build.sh

# 6. Find your image
ls deploy/
# → image_YYYY-MM-DD-ChillCheck.img.xz
```

---

## Flash the Image

### Option A — Raspberry Pi Imager (recommended for Charlie)

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Click **Choose OS** → **Use custom**
3. Select `image_YYYY-MM-DD-ChillCheck.img.xz`
4. Click **Choose Storage** → select the SD card
5. Click **Next** → **No** (don't apply custom settings — image handles this)
6. Click **Write**

### Option B — Command line

```bash
# Find your SD card device
lsblk

# Flash (replace /dev/sdX with your SD card)
xzcat deploy/image_*-ChillCheck.img.xz | sudo dd of=/dev/sdX bs=4M status=progress
sync
```

---

## First Boot Experience

**What happens automatically (no action needed):**

| Time | Event |
|------|-------|
| 0:00 | Pi boots, HDMI shows "ChillCheck is starting up..." |
| 0:30 | System updates |
| 2:00 | Node.js installs |
| 3:00 | Zigbee2MQTT installs |
| 5:00 | Python packages install |
| 6:00 | Services start |
| 6:30 | **http://chillcheck.local is live** |

**What Charlie does:**
1. Plug in: SD card + ethernet cable + ZBDongle-E + power
2. Wait ~6 minutes
3. On any device on the same network: open `http://chillcheck.local`
4. Go to Cloud Link → enter pairing code from dashboard
5. Go to Sensors → pair SNZB-02LD sensors
6. Done ✓

---

## SD Card Contents After Flash

```
/boot/firmware/
└── chillcheck-password.txt    ← SSH password (delete after noting)

/opt/chillcheck/
├── venv/                      ← Python environment
├── subscriber/                ← MQTT subscriber service
└── local_ui/                  ← Flask local UI

/etc/chillcheck/
└── .env                       ← Config (populated by pairing)

/var/log/chillcheck/
├── firstboot.log              ← First boot log
├── subscriber.log             ← Subscriber logs
└── local_ui.log               ← Local UI logs
```

---

## Troubleshooting

**`http://chillcheck.local` not loading after 10 minutes**
```bash
ssh chillcheck@chillcheck.local
# Password in /boot/firmware/chillcheck-password.txt on SD card
cat /var/log/chillcheck/firstboot.log
```

**ZBDongle not detected**
```bash
ls /dev/ttyUSB* /dev/ttyACM*
# Update /opt/zigbee2mqtt/data/configuration.yaml with correct port
sudo systemctl restart zigbee2mqtt
```

**Re-run first boot setup manually**
```bash
sudo rm /etc/chillcheck/.firstboot-complete
sudo systemctl start chillcheck-firstboot
journalctl -u chillcheck-firstboot -f
```

---

## Distributing the Image

If you want to sell ChillCheck as a product:

1. Build the image once on your build machine
2. Upload `image_*-ChillCheck.img.xz` to a file host (S3, Cloudflare R2 etc)
3. Give customers the download link + Raspberry Pi Imager
4. They flash, plug in, open `http://chillcheck.local`, enter pairing code

That's a complete commercial IoT product delivery flow.
