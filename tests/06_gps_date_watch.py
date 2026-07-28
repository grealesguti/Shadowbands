#!/usr/bin/env python3
"""
06_gps_date_watch.py  --  GY-GPS6MV2 (NEO-6M) date / ephemeris acquisition monitor.

PURPOSE:
    A GPS receiver gets the DATE and TIME from the satellite navigation message,
    NOT from you. You cannot set it by hand. Until the module finishes decoding
    the almanac + ephemeris, the RMC date field shows u-blox's stored default
    (12 Dec 2006 -> "121206"), and any position it emits is a quality-6
    dead-reckoning GUESS, not a real fix.

    This tool does ONE thing: watch the RMC/ZDA date and GGA fix-quality, and
    tell you the moment the module transitions to a REAL, time-valid fix.

WHAT "COLD START" MEANS (why the date is wrong):
    Cold start : module knows nothing -- no time, no almanac (~12.5 min to
                 download), no ephemeris (~30 s per satellite). Every weak-signal
                 bit error can restart the almanac cycle. TTFF: minutes.
                 A GY-GPS6MV2 with no working backup cell cold-starts EVERY boot.
    Warm start : almanac valid, ephemeris stale. TTFF ~5-15 s.
    Hot  start : almanac + fresh ephemeris + backup-cell time. TTFF ~1-3 s.

    The wrong date == cold start in progress. Fix it permanently with a CR2032 /
    supercap on the VBAT/backup pad so the RTC + RAM survive power-off.

REAL-FIX SIGNATURE (what this tool waits for):
    * RMC date field shows the CURRENT year (>= 2025), not 2006
    * GGA fix-quality = 1 (GPS) or 2 (DGPS), NOT 6 (dead reckoning)
    * Coordinates near your true location (Montejo ~ 41.3 N, 3.0 W)

WIRING (unchanged from 05):
    VCC->5V  GND->GND  module RX<-Pi GPIO14/TXD(pin8)  module TX->Pi GPIO15/RXD(pin10)

COMMANDS -- prep (only if gpsd is installed and holding the port):
    sudo systemctl stop gpsd gpsd.socket        # harmless "not loaded" error = fine
    sudo cat /dev/serial0                        # raw sanity: should stream $G...
    sudo cat /dev/serial0 | grep -E 'RMC|ZDA'    # watch the date field by hand

COMMANDS -- run THIS monitor:
    # Watch until a valid-date fix appears, default 15 min budget:
    sudo python3 06_gps_date_watch.py

    # Longer budget for a stubborn cold start:
    sudo python3 06_gps_date_watch.py --timeout 1800

    # Different port / baud:
    sudo python3 06_gps_date_watch.py --port /dev/ttyAMA0 --baud 9600

    # Show every raw RMC/ZDA/GGA line as it arrives:
    sudo python3 06_gps_date_watch.py --raw

    # Keep watching forever (no timeout), print a status line each second:
    sudo python3 06_gps_date_watch.py --timeout 0

    # After a valid fix, optionally set the Pi system clock from GPS UTC:
    sudo python3 06_gps_date_watch.py --set-clock
"""

import sys
import time
import argparse
import subprocess
from datetime import datetime, timezone

try:
    import serial
except ImportError:
    print("Needs pyserial:  sudo pip3 install pyserial")
    sys.exit(1)

DEFAULT_DATE_TOKENS = {"121206", "060180", "010180"}  # common u-blox no-time defaults
CURRENT_YEAR_MIN = 2025  # anything below this in the RMC date == not time-valid yet


def checksum_valid(sentence):
    if not sentence.startswith('$') or '*' not in sentence:
        return False
    try:
        data, cksum = sentence[1:].split('*', 1)
        calc = 0
        for ch in data:
            calc ^= ord(ch)
        return f"{calc:02X}" == cksum.strip().upper()
    except Exception:
        return False


def parse_rmc_date(parts):
    """RMC field 9 (index 9) is DDMMYY. Return (yyyy, 'DD-MM-YYYY') or (None, raw)."""
    if len(parts) <= 9 or not parts[9] or len(parts[9]) < 6:
        return None, ""
    raw = parts[9][:6]
    try:
        dd, mm, yy = int(raw[0:2]), int(raw[2:4]), int(raw[4:6])
        yyyy = 2000 + yy  # NEO-6M rolls within 2000-2099
        return yyyy, f"{dd:02d}-{mm:02d}-{yyyy}"
    except ValueError:
        return None, raw


def parse_lat_lon(val, direction):
    if not val or not direction:
        return 0.0
    try:
        dot = val.find('.')
        deg = float(val[:dot - 2])
        minutes = float(val[dot - 2:])
        dec = deg + minutes / 60.0
        return -dec if direction in ('S', 'W') else dec
    except ValueError:
        return 0.0


def gpsd_running():
    try:
        out = subprocess.run(["systemctl", "is-active", "gpsd.socket"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() == "active"
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="GY-GPS6MV2 date/ephemeris acquisition monitor")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--timeout", type=int, default=900,
                    help="Seconds to watch for a valid-date fix; 0 = watch forever (default 900)")
    ap.add_argument("--raw", action="store_true", help="Echo every RMC/ZDA/GGA line")
    ap.add_argument("--set-clock", action="store_true",
                    help="On first valid fix, set the Pi system clock from GPS UTC (needs sudo)")
    args = ap.parse_args()

    print()
    print("  GY-GPS6MV2 date / ephemeris acquisition monitor")
    print(f"  Port {args.port} @ {args.baud}   budget: "
          f"{'unlimited' if args.timeout == 0 else str(args.timeout) + 's'}")
    print("  Waiting for RMC date to leave the 2006 default and become time-valid.\n")

    if gpsd_running():
        print("  ! gpsd.socket is active and may hold the port.")
        print("    -> sudo systemctl stop gpsd gpsd.socket\n")

    try:
        ser = serial.Serial(args.port, baudrate=args.baud, timeout=2.0)
    except Exception as e:
        print(f"  Could not open {args.port}: {e}")
        return

    t0 = time.time()
    last_status = 0.0
    fix_q = "0"
    sats_used = "0"
    lat = lon = 0.0
    valid_year = None
    date_str = ""
    time_str = ""
    reached_valid = False

    try:
        while True:
            if args.timeout and (time.time() - t0) >= args.timeout:
                print("\n  Timeout reached before a valid-date fix.")
                print("  -> Cold start still in progress. Improve sky view / add a backup cell,")
                print("     then re-run with a longer --timeout.")
                break

            line = ser.readline().decode('ascii', errors='ignore').strip()
            if not checksum_valid(line):
                continue

            if args.raw and any(k in line for k in ('RMC', 'ZDA', 'GGA')):
                print(f"      RAW: {line}")

            if 'RMC' in line:
                parts = line.split(',')
                y, ds = parse_rmc_date(parts)
                if y is not None:
                    valid_year, date_str = y, ds
                if len(parts) > 1 and parts[1] and len(parts[1]) >= 6:
                    t = parts[1]
                    time_str = f"{t[0:2]}:{t[2:4]}:{t[4:6]} UTC"

            elif 'GGA' in line:
                parts = line.split(',')
                if len(parts) > 7:
                    fix_q = parts[6] if parts[6] else "0"
                    sats_used = parts[7] if parts[7] else "0"
                    if parts[2] and parts[4]:
                        lat = parse_lat_lon(parts[2], parts[3])
                        lon = parse_lat_lon(parts[4], parts[5])

            # ---- decide state ----
            date_ok = valid_year is not None and valid_year >= CURRENT_YEAR_MIN
            fix_ok = fix_q in ("1", "2")

            now = time.time() - t0
            if now - last_status >= 1.0:
                last_status = now
                if not date_ok:
                    tag = "ACQUIRING (cold start)"
                    note = f"date={date_str or '----'} <- still 2006 default; almanac downloading"
                elif not fix_ok:
                    tag = "DATE VALID, fix provisional"
                    note = f"date={date_str}  fixQ={fix_q} (6=dead-reckoning) sats={sats_used}"
                else:
                    tag = "REAL FIX"
                    note = f"date={date_str}  fixQ={fix_q}  sats={sats_used}  {lat:.5f},{lon:.5f}"
                elapsed = int(now)
                print(f"  [{elapsed:5d}s] {tag:<26s} | {note}")

            # ---- success condition: date valid AND real fix ----
            if date_ok and fix_ok and not reached_valid:
                reached_valid = True
                print("\n  \u2713 TIME-VALID GPS FIX ACQUIRED")
                print(f"    {date_str}  {time_str}")
                print(f"    fix-quality {fix_q}   sats-used {sats_used}")
                print(f"    position {lat:.5f}, {lon:.5f}")
                if args.set_clock and time_str and date_str:
                    try:
                        dd, mm, yyyy = date_str.split('-')
                        hh = parts_time = time_str[:8]
                        iso = f"{yyyy}-{mm}-{dd} {parts_time}"
                        subprocess.run(["date", "-u", "-s", iso], check=True)
                        print(f"    -> system clock set to {iso} UTC")
                    except Exception as e:
                        print(f"    -> could not set clock: {e}")
                print("\n  Continuing to monitor (Ctrl+C to stop)...\n")

    except KeyboardInterrupt:
        print("\n  Monitor stopped.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
