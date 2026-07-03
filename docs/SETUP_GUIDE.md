# Shadow Band Detector — Complete Setup Guide

**Hardware:** Pi Zero W · BMP180 · DS3231 · ADXL335 · 2× OPT101 · OV5647 IR Camera
**OS:** Raspbian GNU/Linux 13 (Trixie)
**User:** eclipse / hostname: eclipse.local

This document records everything that was needed to get the system working,
including all problems encountered and their solutions.

---

## 1. Flash and First Boot

Flash Raspberry Pi OS Lite (32-bit, Trixie) using Raspberry Pi Imager.
In the **Advanced Options** before writing:

```
Hostname:        eclipse
Username:        eclipse
Password:        (your choice)
Enable SSH:      yes (password auth)
Configure Wi-Fi: yes — enter SSID and password
Wi-Fi country:   ES (or FR)
Timezone:        your timezone
```

Insert SD card into Pi Zero W, connect micro-USB to **PWR port**
(the port closest to the mini-HDMI, NOT the one near the SD card).
Wait 90 seconds for first boot.

```bash
ssh eclipse@eclipse.local
```

If .local does not resolve, find the IP from your router device list
or run from Windows PowerShell:
```
arp -a | findstr "b8-27-eb dc-a6-32"
```

---

## 2. Install Script

```bash
cd ~/shadowband
bash install.sh
sudo reboot
```

The install script:
- Installs system packages (tmux, i2c-tools, git, python3-pip)
- Enables SPI, I2C, serial via raspi-config
- Installs Python libraries
- Creates ~/data directory
- Patches config paths for current username

---

## 3. Enable SPI and I2C

```bash
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_serial_hw 0
sudo raspi-config nonint do_serial_cons 1
sudo reboot
```

Verify after reboot:
```bash
ls /dev/spi*    # → /dev/spidev0.0  /dev/spidev0.1
ls /dev/i2c*    # → /dev/i2c-1
```

---

## 4. Camera Setup — OV5647 IR 1080p (Critical Notes)

### 4.1 The connector problem

The Pi Zero W has a **22-pin mini CSI connector** (0.5mm pitch).
Standard Pi cameras ship with a **15-pin full-size cable** (1.0mm pitch).
These are physically different — you need an adapter cable:

```
Camera end:  15-pin, 1.0mm pitch
Pi Zero end: 22-pin, 0.5mm pitch
```

Search: "Raspberry Pi Zero camera cable 15 to 22 pin" (~€2 AliExpress)

### 4.2 Cable insertion

Pi Zero W CSI connector is next to the mini-HDMI port.
- Lift collar STRAIGHT UP (it is fragile, do not force sideways)
- Insert cable with metal contacts facing AWAY from the board
  (toward the USB ports / away from the PCB components)
- Press collar firmly DOWN until it clicks
- Tug gently — cable must not slide out

### 4.3 config.txt — what actually works on Trixie

**Problem:** `camera_auto_detect=1` does NOT reliably detect OV5647 on Pi Zero W
with Raspbian Trixie. `vcgencmd get_camera` shows `supported=0 detected=0`.

**Solution:** Disable auto-detect and specify the overlay manually.

Edit `/boot/firmware/config.txt` (NOT `/boot/config.txt` — Trixie uses firmware/):

```bash
sudo nano /boot/firmware/config.txt
```

Change `camera_auto_detect=1` to `camera_auto_detect=0` and add to the
`[all]` section at the bottom:

```ini
[all]
enable_uart=1
camera_auto_detect=0
start_x=1
gpu_mem=128
dtoverlay=ov5647
```

Full working config.txt bottom section:
```ini
[cm4]
otg_mode=1

[cm5]
dtoverlay=dwc2,dr_mode=host

[all]
enable_uart=1
camera_auto_detect=0
start_x=1
gpu_mem=128
dtoverlay=ov5647
```

### 4.4 Camera detection — correct commands on Trixie

**IMPORTANT:** On Raspbian Trixie, camera tools are `rpicam-*` NOT `libcamera-*`.
`libcamera-hello` is NOT installed by default and is NOT needed.

```bash
# Install if needed (tiny package, just a wrapper)
sudo apt install -y libcamera-apps

# Correct detection command:
rpicam-hello --list-cameras

# Expected output:
# Available cameras
# -----------------
# 0 : ov5647 [2592x1944 10-bit GBRG] (/base/soc/i2c0mux/i2c@1/ov5647@36)
#     Modes: 'SGBRG10_CSI2P' : 640x480 [58.92 fps]
#                              1296x972 [46.34 fps]
#                              1920x1080 [32.81 fps]
#                              2592x1944 [15.63 fps]
```

**NOTE:** `vcgencmd get_camera` shows `detected=0` even when working —
this is NORMAL on Trixie with libcamera stack. Ignore it.
Use `rpicam-hello --list-cameras` instead.

**NOTE:** OV5647 appears at address `0x36` (shown as `UU`) on I2C bus 10:
```bash
sudo i2cdetect -y 10
# Shows UU at 0x36 = kernel driver has claimed it = working correctly
```

### 4.5 Verified working capture commands

```bash
# Still image — WORKS
rpicam-still -o ~/data/test.jpg --width 1920 --height 1080 \
  -t 2000 --nopreview 2>/dev/null

# Video — WORKS
rpicam-vid -o ~/data/test.h264 --width 1920 --height 1080 \
  --framerate 30 -t 5000 --nopreview 2>/dev/null
```

### 4.6 Suppress verbose output

rpicam commands print INFO logs to stderr by default. Suppress with:
```bash
2>/dev/null
```

Or set environment variable for all commands in a session:
```bash
export LIBCAMERA_LOG_LEVELS=ERROR
```

### 4.7 Aliases for compatibility

The project scripts use `libcamera-*` naming. Add aliases:
```bash
echo "alias libcamera-still='rpicam-still'" >> ~/.bashrc
echo "alias libcamera-vid='rpicam-vid'" >> ~/.bashrc
echo "alias libcamera-hello='rpicam-hello'" >> ~/.bashrc
source ~/.bashrc
```

---

## 5. DS3231 RTC Setup

Wire to I²C:
```
DS3231 VCC → Pi pin 1  (3.3V)
DS3231 GND → Pi pin 6  (GND)
DS3231 SDA → Pi pin 3  (GPIO2)
DS3231 SCL → Pi pin 5  (GPIO3)
```

Enable hardware clock support:
```bash
echo "dtoverlay=i2c-rtc,ds3231" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

Sync RTC from internet time (do this at home before eclipse):
```bash
sudo hwclock -w    # write system time to RTC
sudo hwclock -r    # read back to verify
```

Verify at boot without internet:
```bash
sudo timedatectl set-ntp false
sudo timedatectl set-time "2000-01-01 00:00:00"
sudo hwclock -s    # restore from RTC
date               # should show correct time
sudo timedatectl set-ntp true
```

---

## 6. Wiring Reference

### I²C bus (SDA = pin 3, SCL = pin 5)
```
BMP180:  VCC→pin1  GND→pin6  SDA→pin3  SCL→pin5   addr 0x77
DS3231:  VCC→pin1  GND→pin6  SDA→pin3  SCL→pin5   addr 0x68
```

### MCP3208 SPI (when purchased)
```
pin 16 VDD  → Pi 3.3V (pin 1)
pin 15 VREF → Pi 3.3V (pin 1)
pin 14 AGND → Pi GND  (pin 6)
pin  9 DGND → Pi GND  (pin 6)
pin 13 CLK  → Pi GPIO11 SCLK (pin 23)
pin 11 DIN  → Pi GPIO10 MOSI (pin 19)
pin 12 DOUT → Pi GPIO9  MISO (pin 21)
pin 10 /CS  → Pi GPIO8  CE0  (pin 24)
```

### OPT101 (per sensor)
```
pin 2 (+Vs)  → Pi 3.3V
pin 3 (GND)  → Pi GND
pin 1 (VOUT) → MCP3208 CH0 (sensor #1) or CH1 (sensor #2)
pins 5, 8    → leave OPEN (internal 1MΩ feedback)
```

### ADXL335
```
VCC  → Pi 3.3V
GND  → Pi GND
XOUT → MCP3208 CH5
YOUT → MCP3208 CH6
ZOUT → MCP3208 CH7
```

### Decoupling capacitors (from kit)
```
47µF across MCP3208 VDD (pin16) and AGND (pin14)  ← most important
10µF across MCP3208 VREF (pin15) and AGND (pin14)
10µF per OPT101: pin2 (+Vs) to pin3 (GND)
10µF across ADXL335 VCC and GND
```

---

## 7. Field Connection (No Wi-Fi)

Configure Pi as hotspot for eclipse site:
```bash
sudo apt install -y hostapd dnsmasq
```

See docs/HOTSPOT_SETUP.md for full configuration.

At eclipse site:
- Connect laptop to Wi-Fi network: shadowband
- Password: eclipse2026
- SSH: ssh eclipse@192.168.4.1

---

## 8. Known Issues and Solutions

| Issue | Cause | Solution |
|---|---|---|
| `vcgencmd get_camera` shows detected=0 | Normal on Trixie with libcamera | Use `rpicam-hello --list-cameras` instead |
| `libcamera-hello: command not found` | Not default on Trixie | Use `rpicam-hello` or `sudo apt install libcamera-apps` |
| `camera_auto_detect=1` not detecting OV5647 | Pi Zero W + Trixie incompatibility | Set to 0 and add `dtoverlay=ov5647` manually |
| Camera `supported=0` | Config not applied | Check `/boot/firmware/config.txt` not `/boot/config.txt` |
| Camera `supported=1 detected=0` | Physical cable issue or wrong cable | Reseat cable, check 22-pin adapter for Zero W |
| `0x36 UU` on i2cdetect bus 10 | Normal — kernel claimed address | Camera working correctly |
| Wi-Fi lost after config edit | Lines outside `[all]` block | Ensure all custom lines inside `[all]` section |
| DS3231 and MPU-6050 both 0x68 | Address conflict | Pull MPU-6050 AD0 HIGH → moves to 0x69 |
| Throttling `0x50005` | Weak USB cable/charger | Use quality cable, check power supply |
