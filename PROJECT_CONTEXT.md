# PROJECT_CONTEXT.md — Shadowband Eclipse Project
> Paste or attach this file at the start of any AI-assisted session.
> Last updated: 2026-07-03 · Eclipse: 2026-08-12, Montejo de Tiermes, Soria

## Mission
Citizen-science shadowband observation during the Aug 2026 total solar eclipse.
Detect ~4.5 Hz shadow band signal via photodiode arrays + sky camera.
Software: `shadowband_v5` (tests 01–08, recording, analysis, calibration).

## Hardware inventory
| Unit | Board | Role | Hostname / user | Status |
|---|---|---|---|---|
| Primary | Pi Zero W | 8× OPT101 L-array (currently 2), MCP3208 SPI, BMP180, DS3231, ADXL335, OV5647 IR cam | `eclipse` / `eclipse` | ⚠️ SD boot failure (see Known Issues) |
| Secondary | Pi 3B+ | Second camera + sensors (BPW34/MCP6002 station planned) | `eclipse3` / `eclipse3` | Active, on WiFi + Ethernet |
| Backup | Pi 4 (office) | To bring | — | Not yet retrieved |

Sampling: 400 Hz, FFT spectrogram targeting ~4.5 Hz. OS on both: Raspberry Pi OS **Trixie** (Debian 13).

## Network access matrix (as of 2026-07-03)
| Path | Command | Notes |
|---|---|---|
| 3B+ via Ethernet direct to laptop | `ssh eclipse3@169.254.10.2` | **Static IP pinned on eth0** — field-site method, no router needed. Laptop side auto-assigns 169.254.x.x (APIPA). |
| 3B+ via mDNS | `ssh eclipse3@eclipse3.local` | Works over WiFi or cable |
| 3B+ via home WiFi | Router assigns 192.168.1.x | SSID `MOVISTAR_F84C` (2.4 GHz, WPA2/AES) |
| Zero W via WiFi | `ssh eclipse@eclipse.local` | Once SD fixed. Networks configured: MOVISTAR_F84C + SFR_F05F |
| Zero W via USB gadget | `ssh eclipse@eclipse.local` over data-port cable | Configured (see below), untested pending SD fix |

Home router: Movistar, admin at 192.168.1.1. Laptop: Windows 11 24H2 (build 26200) + WSL2. Pi MAC prefix: `b8-27-eb` (identify in `arp -a` / router list).

## USB gadget config (Zero W ONLY — does NOT work on 3B+/Pi4, host-only USB)
- `config.txt` under `[all]`: `dtoverlay=dwc2`
- `cmdline.txt` (single line, space-separated, after regdom): `modules-load=dwc2,g_ncm`
- **Must be `g_ncm`, NOT `g_ether`** — Windows 11 24H2 removed the RNDIS driver; NCM is native.
- `network-config` usb0 block: `ethernets: usb0: {optional: true, link-local: [ipv4], dhcp4: false}`
- Data goes in the **inner USB port** (next to mini-HDMI); PWR port has no data lines.

## Cloud-init rules (Trixie Imager images — boot partition F:\)
- WiFi/eth config lives in `network-config` (netplan v2 YAML), hostname/users/SSH in `user-data`.
- **Edits to `network-config` are IGNORED unless `instance-id` in `meta-data` is changed** (any new string). Current: `rpi-imager-1780845198102-newwifi2`.
- Instance-id bump re-runs per-instance modules: may regenerate SSH host keys (`ssh-keygen -R <host>` on laptop if warned) and retries `packages: avahi-daemon` (slower boot).
- Zero W is 2.4 GHz only, WPA2 ok, WPA3-only networks invisible to it. SSIDs case-sensitive.

## Camera stack (CONFIRMED WORKING recipe, Trixie)
- `config.txt` `[all]`: `camera_auto_detect=0`, `start_x=1`, `gpu_mem=128`, `dtoverlay=ov5647`
- Use `rpicam-*` commands (NOT `libcamera-*` on Trixie). Detection: `rpicam-hello --list-cameras` (NOT `vcgencmd get_camera`).
- I2C 0x36 showing as `UU` on bus 10 is CORRECT.
- 3B+ uses the wide 15-pin ribbon (Zero uses narrow 22-pin); adapters differ.
- Also on config.txt: `dtoverlay=i2c-rtc,ds3231`, `enable_uart=1`, `dtparam=i2c_arm=on`, `dtparam=spi=on`.

## Known issues / open items
1. **Zero W does not boot from its SD card** (2026-07-03). Symptoms: steady ACT LED (no SD activity), Windows enumerates `VID_0A5C&PID_2763` = BCM boot-ROM USB fallback = first-stage bootloader never loaded. Cable and Pi data port PROVEN GOOD (enumeration works). Pending: deliberate reseat, contact cleaning, `chkdsk F: /f`, WSL2 `e2fsck` on rootfs + **backup /home/eclipse**, spare-card cross-test (card-vs-slot verdict). The 3B+ can also mount/fsck the card via USB reader.
2. Zero W config files on F:\ verified line-by-line correct (cmdline single-line fix applied: `ES modules-load=...` space not semicolon).
3. WiFi password for MOVISTAR_F84C re-entered from router admin (was a suspect, now presumed correct but unverified until Zero boots).

## Diagnostic cheat-sheet (learned this iteration)
| Observation | Meaning |
|---|---|
| ACT LED irregular flicker | Booting normally |
| ACT LED repeating N-blink pattern | Firmware error (4=start.elf missing, 7=kernel missing) |
| ACT LED steady, no flicker | Boot ROM can't read SD at all |
| Windows sees VID 0A5C / PID 2763 | Pi in boot-ROM USB mode = SD not booting; also proves cable+port good |
| No USB event at all in PnP log | Charge-only cable or dead port. Check: `powershell -Command "Get-WinEvent -LogName 'Microsoft-Windows-Kernel-PnP/Configuration' -MaxEvents 30 \| Format-Table TimeCreated, Id, Message -Wrap"` |
| `arp -a` MAC starting b8-27-eb | A Raspberry Pi |
| MAC 2nd hex digit a/e/2/6 (e.g. 1a-...) | Randomized MAC = phone/tablet, not a Pi |
| "Could not resolve hostname" | mDNS/no link (different from timeout = link but no SSH) |
| Windows sweep to populate arp | `for /L %i in (2,1,254) do @ping -n 1 -w 100 192.168.1.%i > nul` then `arp -a` |

## Standard procedures
- **Deploy new version:** `scp shadowband_vN.zip eclipse3@169.254.10.2:/home/eclipse3/` → ssh in → `rm -rf ~/shadowband && unzip ~/shadowband_vN.zip && mv ~/shadowband_vN ~/shadowband && rm ~/shadowband_vN.zip && cd ~/shadowband && bash install.sh` (install.sh sed-fixes /home/eclipse→current user) → reboot → `python3 run_tests.py`.
- **Always work inside tmux:** `tmux new -s main` / reattach `tmux attach -t main`.
- **Field connection drill:** laptop↔Pi Ethernet cable, `ssh eclipse3@169.254.10.2`; verify eth0 with `ip -br addr`, force-path test `ping -I eth0 <laptop 169.254.x.x>`.

## Test log
- 2026-06-07 (Zero W): Test 01 — 11/11 PASS (49.2°C, 8.0 MB/s SD, 24.2 GB free). Camera confirmed via rpicam-vid/still after manual config.txt (no auto-detection). Clock working. Phone-hotspot boot+connection verified.
- Pending on 3B+: full run_tests.py after v5 deploy; camera test 07 with second OV5647.

## To buy / bring
128 GB card (check compatibility), Pi 4 from office, wide-angle camera, speakers, wind speed sensor, sunlight sensor, second tripod, USB-TTL serial adapter (3.3 V, rescue console), spare microSD cards, known-good micro-USB data cable (label it!).
