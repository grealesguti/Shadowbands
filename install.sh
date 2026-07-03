#!/bin/bash
# install.sh — One-shot setup for Pi Zero W
# Run after first SSH login:  bash install.sh
# ─────────────────────────────────────────────────────────────
set -e

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Shadow Band Detector — Pi Zero W Setup        ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── System packages ───────────────────────────────────────────
echo "[1/5] Installing system packages..."
sudo apt-get update -q
sudo apt-get install -y -q \
    python3-pip python3-dev \
    i2c-tools \
    git \
    tmux \
    unzip

# ── Enable interfaces ─────────────────────────────────────────
echo "[2/5] Enabling SPI and I2C..."
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_serial_hw 0
sudo raspi-config nonint do_serial_cons 1

# Add current user to spi and i2c groups
sudo usermod -aG spi,i2c "$USER"

# ── Python packages ───────────────────────────────────────────
echo "[3/5] Installing Python packages..."
pip3 install --break-system-packages --quiet \
    spidev \
    RPi.GPIO \
    PyYAML \
    smbus2 \
    adafruit-blinka \
    adafruit-circuitpython-ds3231

# ── Data directory ────────────────────────────────────────────
echo "[4/5] Creating data directory..."
mkdir -p ~/data

# ── Config path fix ───────────────────────────────────────────
echo "[5/5] Setting config username..."
# Replace any hardcoded 'pi' paths with current user
CONF=~/shadowband/config/config.yaml
if [ -f "$CONF" ]; then
    sed -i "s|/home/pi/|/home/$USER/|g" "$CONF"
    sed -i "s|/home/eclipse/|/home/$USER/|g" "$CONF"
    echo "  Config updated for user: $USER"
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Installation complete                         ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  IMPORTANT: Reboot before running tests."
echo ""
echo "    sudo reboot"
echo ""
echo "  After reboot, reconnect and run:"
echo ""
echo "    cd ~/shadowband"
echo "    python3 run_tests.py"
echo ""
