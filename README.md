# Shadow Band Detector v5

**Hardware confirmed working:**
- Raspberry Pi Zero W
- OV5647 IR Camera 3.6mm 1080p
- BMP180 pressure+temperature sensor
- DS3231 RTC
- BK-ADXL335 accelerometer
- 2× OPT101 photodiodes (expand to 8 before eclipse)

**OS:** Raspbian GNU/Linux 13 (Trixie)
**User:** eclipse / **Hostname:** eclipse.local

---

## Critical: camera uses rpicam-* not libcamera-*

On Raspbian Trixie the camera stack is `rpicam-*`:
- Detection: `rpicam-hello --list-cameras`
- Still:     `rpicam-still -o file.jpg ...`
- Video:     `rpicam-vid -o file.h264 ...`

`vcgencmd get_camera` shows `detected=0` even when working — **ignore it**.
`sudo i2cdetect -y 10` showing `UU` at `0x36` = camera is working.

See `docs/SETUP_GUIDE.md` for full setup history and all problems solved.

---

## Quick start on a new Pi

```bash
# Copy project
scp -r shadowband/ eclipse@eclipse.local:/home/eclipse/

# SSH in and install
ssh eclipse@eclipse.local
cd ~/shadowband && bash install.sh
sudo reboot

# After reboot — run tests in order
cd ~/shadowband
python3 run_tests.py
```

---

## Test order

| Test | Hardware needed |
|---|---|
| 01 | Nothing |
| 02 | BMP180 + DS3231 on I²C |
| 03 | MCP3208 + 2×10kΩ on CH0 |
| 04 | 2× OPT101 on CH0+CH1 |
| 05 | ADXL335 on CH5+CH6+CH7 |
| 06 | All sensors together |
| 07 | Camera (CSI flat cable) |
| 08 | Camera + all sensors |

---

## Transfer commands

```bash
# Laptop → Pi (sync project)
rsync -avz ~/shadowband/ eclipse@eclipse.local:/home/eclipse/shadowband/

# Pi → Laptop (get data)
scp eclipse@eclipse.local:~/data/*.csv ~/downloads/
scp eclipse@eclipse.local:~/data/*.jpg ~/downloads/
```

## Field connection (no Wi-Fi)

See docs/SETUP_GUIDE.md section 7 for hotspot configuration.
After setup: connect to Wi-Fi "shadowband", password "eclipse2026"
SSH: `ssh eclipse@192.168.4.1`
