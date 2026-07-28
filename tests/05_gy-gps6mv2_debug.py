#!/usr/bin/env python3
"""
05_gy-gps6mv2_debug.py  --  GY-GPS6MV2 (NEO-6M) bring-up & time/position sync test (UART).

GNSS receiver over UART -- leaves all SPI channels (MCP3208) and I2C buses untouched.
Provides absolute UTC time frames and coordinate baselines for solar az/alt calc.

WIRING:
    GY-GPS6MV2 VCC -> 5 V         (onboard regulator drops to 3.3 V safely)
    GY-GPS6MV2 GND -> GND
    GY-GPS6MV2 RX  -> Pi GPIO 14 (TXD - Physical Pin 8)   (crossed Rx<-Tx)
    GY-GPS6MV2 TX  -> Pi GPIO 15 (RXD - Physical Pin 10)  (crossed Tx->Rx)
    GY-GPS6MV2 PPS -> Pi GPIO 18 (Physical Pin 12)        [Optional solder tap]

ANTENNA:
    Ceramic patch antenna: CERAMIC SQUARE FACE POINTS UP (flat to the sky).
    Connector/pins face down. Give it an unobstructed 360deg horizon -- not on a
    windowsill or against a wall (that halves the sky and can prevent a first fix).

SETUP (one time):
    sudo raspi-config      -> Interface Options -> Serial Port:
                              - Login shell over serial: NO
                              - Serial port hardware enabled: YES
                              Reboot after setting!
    sudo pip3 install pyserial

COMMANDS -- raw bring-up / manual debugging (run these BEFORE the script if unsure):
    # 1) Free the serial port (gpsd grabs it and blocks raw reads):
    sudo systemctl stop gpsd gpsd.socket

    # 2) Raw sentence sanity check -- should stream $GPRMC / $GPGGA / $GPGSV:
    sudo cat /dev/serial0
    #    ...garbage/nothing => baud mismatch or TX/RX still swapped.

    # 3) Satellite signal strength even with NO fix (this is the key test):
    sudo cat /dev/serial0 | grep GPGSV
    #    SNR 0 on all sats => antenna sees nothing (facing down / blocked / dead).
    #    SNR 20-45 on several sats => acquiring, just needs time.

    # 4) Confirm the device node exists:
    ls -l /dev/serial0

COMMANDS -- run THIS script:
    # Default run (auto-frees gpsd is NOT done for you; stop it first if needed):
    sudo python3 05_gy-gps6mv2_debug.py

    # Longer cold-start fix window (recommended first time, no coin-cell backup):
    sudo python3 05_gy-gps6mv2_debug.py --fix-timeout 900

    # Different port / baud:
    sudo python3 05_gy-gps6mv2_debug.py --port /dev/ttyAMA0 --baud 9600

    # Skip the live telemetry loop (diagnostics only, then exit):
    sudo python3 05_gy-gps6mv2_debug.py --no-live

    # Skip PPS stage (no wire soldered):
    sudo python3 05_gy-gps6mv2_debug.py --no-pps

    # Dump every raw NMEA line during acquisition (verbose troubleshooting):
    sudo python3 05_gy-gps6mv2_debug.py --raw

WHAT THIS PROVIDES (and does NOT):
    PROVIDES: absolute UTC timestamp (NMEA ~0.1-1 s, PPS <1 us) and exact Lat/Lon.
    DOES NOT: immediate absolute orientation. Azimuth must be derived by tracking
              shadow nodes or solving sun paths.

FIRST-FIX EXPECTATION:
    Most GY-GPS6MV2 boards have NO battery backup, so every power-up is a COLD
    start: 30 s to 15 min with clear sky is normal. A VOID/no-fix result in the
    first minute means nothing -- watch the sat count / SNR climb instead.
"""

import os
import sys
import time
import argparse
import subprocess

try:
    import serial
except ImportError:
    print("Needs pyserial:  sudo pip3 install pyserial")
    sys.exit(1)

# Check for RPi.GPIO to monitor the optional PPS hardware line
PPS_AVAILABLE = False
try:
    import RPi.GPIO as GPIO
    PPS_AVAILABLE = True
except ImportError:
    pass


def ok(d, x=""):   print(f"  \u2713 [PASS]  {d:<44s} {x}")
def bad(d, x=""):  print(f"  \u2717 [FAIL]  {d:<44s} {x}")
def warn(d, x=""): print(f"  ! [WARN]  {d:<44s} {x}")
def info(d):       print(f"    {d}")


def parse_nmea_lat_lon(val, direction):
    """Convert NMEA coordinate format (DDMM.MMMM) to decimal degrees."""
    if not val or not direction:
        return 0.0
    try:
        dot = val.find('.')
        degrees = float(val[:dot - 2])
        minutes = float(val[dot - 2:])
        decimal = degrees + (minutes / 60.0)
        if direction in ('S', 'W'):
            decimal = -decimal
        return decimal
    except ValueError:
        return 0.0


def checksum_valid(sentence):
    """Validate NMEA sentence checksum string (*XX)."""
    if not sentence.startswith('$') or '*' not in sentence:
        return False
    try:
        data, cksum = sentence[1:].split('*', 1)
        calc = 0
        for char in data:
            calc ^= ord(char)
        return f"{calc:02X}" == cksum.strip().upper()
    except Exception:
        return False


def parse_gsv_snr(lines):
    """
    Parse $GxGSV sentences and return (sats_tracked, max_snr, snr_list).
    GSV format: $GPGSV,total_msgs,msg_num,sats_in_view, [prn,elev,azim,snr]x4 *cs
    SNR is the 4th field of each 4-field satellite group; blank = not tracked.
    """
    sats_tracked = 0
    snr_list = []
    for line in lines:
        if 'GSV' not in line:
            continue
        parts = line.split(',')
        # satellite groups start at index 4, in blocks of 4
        i = 4
        while i + 3 < len(parts):
            snr_field = parts[i + 3]
            # last field of the sentence may carry the *checksum -- strip it
            snr_field = snr_field.split('*')[0].strip()
            if snr_field.isdigit():
                snr = int(snr_field)
                if snr > 0:
                    sats_tracked += 1
                    snr_list.append(snr)
            i += 4
    max_snr = max(snr_list) if snr_list else 0
    return sats_tracked, max_snr, snr_list


def gpsd_running():
    """Best-effort check whether gpsd holds the port (would block raw reads)."""
    try:
        out = subprocess.run(["systemctl", "is-active", "gpsd.socket"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() == "active"
    except Exception:
        return False


def read_lines(ser, seconds, want=None, raw=False):
    """Read valid NMEA lines for a window; stop early once 'want' collected."""
    out = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        line = ser.readline().decode('ascii', errors='ignore').strip()
        if raw and line:
            print(f"      RAW: {line}")
        if line.startswith('$'):
            out.append(line)
            if want is not None and len(out) >= want:
                break
    return out


def main():
    ap = argparse.ArgumentParser(description="GY-GPS6MV2 (NEO-6M) UART bring-up & diagnostics")
    ap.add_argument("--port", type=str, default="/dev/serial0", help="Serial interface (default /dev/serial0)")
    ap.add_argument("--baud", type=int, default=9600, help="NEO-6M factory default baud rate (9600)")
    ap.add_argument("--pps-pin", type=int, default=18, help="BCM pin for hardware PPS tracking (18)")
    ap.add_argument("--fix-timeout", type=int, default=120,
                    help="Seconds to wait for a satellite fix (default 120; use 600-900 for cold start)")
    ap.add_argument("--pps-timeout", type=int, default=3, help="Seconds to wait for a PPS edge (default 3)")
    ap.add_argument("--no-live", action="store_true", help="Skip the live telemetry loop; exit after diagnostics")
    ap.add_argument("--no-pps", action="store_true", help="Skip the PPS hardware stage entirely")
    ap.add_argument("--raw", action="store_true", help="Print every raw NMEA line during acquisition")
    args = ap.parse_args()

    print()
    print("\u2554" + "\u2550" * 54 + "\u2557")
    print("\u2551  05 \u2014 GY-GPS6MV2 bring-up & time/position test       \u2551")
    print("\u255a" + "\u2550" * 54 + "\u255d")
    print(f"\n  UART Port: {args.port} @ {args.baud} Baud")
    print(f"  Fix window: {args.fix_timeout}s   PPS window: {args.pps_timeout}s")
    print("  Provides absolute coordinates and UTC time frames.\n")

    if gpsd_running():
        warn("gpsd.socket is ACTIVE", "It may hold the port and block reads.")
        info("\u2192 Free it first:  sudo systemctl stop gpsd gpsd.socket")

    passes, total = 0, 6

    # ---------- STAGE 0: Serial interface accessibility ----------
    print("  STAGE 0 \u2014 PORT ACCESS")
    if not os.path.exists(args.port):
        bad("UART interface mapping", f"Port {args.port} not found.")
        info("\u2192 Check raspi-config: console-login OFF, serial hardware ON, then reboot.")
        return

    try:
        ser = serial.Serial(args.port, baudrate=args.baud, timeout=2.0)
        ok("UART link opened successfully", args.port)
        passes += 1
    except Exception as e:
        bad("UART link opened successfully", str(e))
        info("\u2192 If 'device busy', gpsd is holding it. Stop gpsd and retry.")
        return

    # ---------- STAGE 1: NMEA Stream Presence ----------
    print("\n  STAGE 1 \u2014 DATA STREAM READING")
    sentences = read_lines(ser, 3.0, want=8, raw=args.raw)
    if len(sentences) >= 3:
        ok("NMEA character stream captured", f"{len(sentences)} lines read.")
        passes += 1
    else:
        bad("NMEA character stream captured", "No structural sentences received.")
        info("\u2192 Verify TX/RX crossed (Pi TXD->module RX, module TX->Pi RXD).")
        info("\u2192 Wrong baud shows garbage; try:  sudo cat /dev/serial0")
        ser.close()
        return

    # ---------- STAGE 2: Protocol Checksum Integrity ----------
    print("\n  STAGE 2 \u2014 CHECKSUM VERIFICATION")
    valid_count = sum(1 for s in sentences if checksum_valid(s))
    if valid_count > 0:
        ok("Sentence formatting checks out", f"{valid_count}/{len(sentences)} passed checksum.")
        passes += 1
    else:
        bad("Sentence formatting checks out", "All packets failed checksum.")
        info("\u2192 Signal noise or wrong baud rate. Confirm 9600 for NEO-6M.")

    # ---------- STAGE 3: Antenna / Sky Visibility (GSV SNR) ----------
    # This is the decisive antenna test: it works even with NO fix.
    print("\n  STAGE 3 \u2014 ANTENNA / SKY VISIBILITY (GSV SNR)")
    info("Sampling $GxGSV for satellite signal-to-noise (needs sky view)...")
    gsv_lines = [s for s in read_lines(ser, 6.0, raw=args.raw) if 'GSV' in s and checksum_valid(s)]
    sats_tracked, max_snr, snr_list = parse_gsv_snr(gsv_lines)

    if sats_tracked >= 3 and max_snr >= 15:
        ok("Antenna receiving satellites", f"{sats_tracked} sats, max SNR {max_snr} dBHz.")
        passes += 1
    elif sats_tracked >= 1:
        warn("Antenna receiving satellites", f"Only {sats_tracked} sat(s), max SNR {max_snr} dBHz.")
        info("\u2192 Weak sky view. Move antenna to open 360deg horizon; wait longer.")
        passes += 1  # something is being heard -- not a hardware fail
    else:
        bad("Antenna receiving satellites", "0 sats tracked (all SNR 0).")
        info("\u2192 Ceramic square face must point UP, flat to open sky.")
        info("\u2192 If still 0 after 15 min outdoors: check u.FL pigtail / dead module.")
    if snr_list:
        info(f"SNR sample: {sorted(snr_list, reverse=True)}")

    # ---------- STAGE 4: Satellite Fix Acquisition (with countdown) ----------
    print("\n  STAGE 4 \u2014 SATELLITE FIX ACQUISITION")
    info(f"Waiting up to {args.fix_timeout}s for a valid fix (cold start can take minutes)...")
    has_lock = False
    scan_start = time.time()
    last_report = 0.0

    while time.time() - scan_start < args.fix_timeout:
        line = ser.readline().decode('ascii', errors='ignore').strip()
        if args.raw and line:
            print(f"      RAW: {line}")
        if not checksum_valid(line):
            continue

        # Live acquisition progress from GGA (fix quality + sats used)
        if 'GGA' in line:
            parts = line.split(',')
            if len(parts) > 7:
                fix_q = parts[6] if parts[6] else "0"   # 0=no fix,1=GPS,2=DGPS
                sats_used = parts[7] if parts[7] else "0"
                now = time.time() - scan_start
                if now - last_report >= 2.0:
                    remaining = int(args.fix_timeout - now)
                    print(f"    [{remaining:4d}s left]  fix-quality={fix_q}  sats-used={sats_used}", end="\r")
                    last_report = now

        if 'RMC' in line:
            parts = line.split(',')
            if len(parts) > 2 and parts[2] == 'A':   # A=Active/Valid
                has_lock = True
                break

    print()  # clear the \r line
    if has_lock:
        ok("GNSS satellite fix secured", "Active 2D/3D positioning live.")
        passes += 1
    else:
        warn("GNSS satellite fix secured", "No valid fix within timeout (VOID).")
        info("\u2192 Not necessarily a failure: extend --fix-timeout and ensure clear sky.")
        info(f"\u2192 Stage 3 showed {sats_tracked} sats tracked; a fix needs ~4 with good SNR.")

    # ---------- STAGE 5: Hardware PPS Line Validation ----------
    print("\n  STAGE 5 \u2014 PPS HARDWARE TICK INTERRUPT")
    if args.no_pps:
        warn("PPS line monitoring status", "Skipped (--no-pps).")
    elif not PPS_AVAILABLE:
        warn("PPS line monitoring status", "Skipped. RPi.GPIO module missing.")
    elif not has_lock:
        warn("PPS line monitoring status", "Skipped: PPS only pulses after a fix.")
        info("\u2192 Get a satellite fix first, then re-run to validate PPS.")
    else:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(args.pps_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        info(f"Listening for 1 Hz pulse edges on BCM pin {args.pps_pin}...")
        edge = GPIO.wait_for_edge(args.pps_pin, GPIO.RISING, timeout=args.pps_timeout * 1000)
        if edge is not None:
            ok("Hardware PPS flank registered", "Sub-microsecond UTC sync viable.")
            passes += 1
        else:
            warn("Hardware PPS flank registered", "No pulse edge intercepted.")
            info("\u2192 OK if PPS wire not soldered. Otherwise check GPIO18 solder tap.")

    # ---------- RESULTS SUMMARY ----------
    print("\n  " + "\u2500" * 56)
    print(f"  {passes}/{total} diagnostic tracks passed.")
    if passes >= total - 1 and has_lock:
        print("  \u2713 Clock and space telemetry pipeline validated.")
    else:
        print("  ! Review flagged diagnostics before field deployment.")

    # ---------- LIVE STREAM FEED ----------
    if args.no_live:
        ser.close()
        if PPS_AVAILABLE and not args.no_pps:
            GPIO.cleanup()
        return

    print("\n  LIVE TELEMETRY STREAM (Ctrl+C to terminate):")
    try:
        lat, lon, sat_utc = "0.00000", "0.00000", "N/A"
        while True:
            line = ser.readline().decode('ascii', errors='ignore').strip()
            if not checksum_valid(line):
                continue

            if 'GGA' in line:
                parts = line.split(',')
                if len(parts) > 7:
                    sat_utc = parts[1] if parts[1] else "N/A"
                    fix_q = parts[6] if parts[6] else "0"
                    sats_used = parts[7] if parts[7] else "0"
                    if parts[2] and parts[4]:
                        lat = f"{parse_nmea_lat_lon(parts[2], parts[3]):.5f}"
                        lon = f"{parse_nmea_lat_lon(parts[4], parts[5]):.5f}"
                    if sat_utc != "N/A" and len(sat_utc) >= 6:
                        sat_utc = f"{sat_utc[:2]}:{sat_utc[2:4]}:{sat_utc[4:6]} UTC"
                    print(f"   [GGA] {sat_utc:<12s} | Lat {lat:<10s} | Lon {lon:<10s} | "
                          f"fixQ {fix_q} | SatsUsed {sats_used}")

            elif 'RMC' in line:
                parts = line.split(',')
                if len(parts) > 7:
                    status = "FIX VALID" if parts[2] == 'A' else "NO FIX"
                    speed = parts[7] if parts[7] else "0.0"
                    print(f"   [RMC] {status:<9s} | Speed {speed} knots")

            elif 'GSV' in line:
                st, ms, _ = parse_gsv_snr([line])
                if st:
                    print(f"   [GSV] tracking {st} sat(s), max SNR {ms} dBHz")

    except KeyboardInterrupt:
        print("\n  Live monitoring interrupted.")
    finally:
        ser.close()
        if PPS_AVAILABLE and not args.no_pps:
            GPIO.cleanup()


if __name__ == "__main__":
    main()
