#!/usr/bin/env python3
"""
tests/02_i2c_bmp180_ds3231.py
──────────────────────────────
Test 2 — I²C bus, BMP180 pressure sensor, DS3231 RTC.

Wiring:
  Pi pin 1  (3.3V) → VCC on both BMP180 and DS3231
  Pi pin 6  (GND)  → GND on both
  Pi pin 3  (GPIO2 SDA) → SDA on both   ← same wire
  Pi pin 5  (GPIO3 SCL) → SCL on both   ← same wire

  BMP180 I²C address: 0x77
  DS3231 I²C address: 0x68

Usage: python3 tests/02_i2c_bmp180_ds3231.py
       python3 tests/02_i2c_bmp180_ds3231.py --sync-rtc
"""
import sys, time, subprocess, argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, BMP180, DS3231

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<40} {detail}")
    return ok

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync-rtc", action="store_true",
                        help="Sync DS3231 time from system clock")
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    setup_logging(cfg)

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 2 — I²C: BMP180 + DS3231                 ║")
    print("╚══════════════════════════════════════════════════╝")
    print("\n  Both devices wired to SDA (pin 3) and SCL (pin 5)\n")

    results = []

    # ── I²C scan ─────────────────────────────────────────────
    print("  I²C BUS SCAN")
    try:
        r = subprocess.run(["i2cdetect", "-y", "1"],
                           capture_output=True, text=True, timeout=5)
        found = []
        for line in r.stdout.split("\n"):
            for tok in line.split():
                try: found.append(int(tok, 16))
                except ValueError: pass

        print(f"  Addresses found: {[hex(a) for a in found]}\n")
        results.append(chk("I²C bus active", len(found) > 0,
                            f"{len(found)} device(s) found"))
        results.append(chk("BMP180 @ 0x77", 0x77 in found,
                            "found ✓" if 0x77 in found else
                            "NOT FOUND — check VCC/GND/SDA/SCL wiring"))
        results.append(chk("DS3231 @ 0x68", 0x68 in found,
                            "found ✓" if 0x68 in found else
                            "NOT FOUND — check CR2032 installed in RTC module"))
        if 0x76 in found and 0x77 not in found:
            print("  NOTE: 0x76 found — you may have a BME280 not BMP180 (that's fine!)")
    except FileNotFoundError:
        print("  i2cdetect not found → sudo apt install i2c-tools")
        results.append(chk("i2cdetect", False, "install: sudo apt install i2c-tools"))

    # ── BMP180 ───────────────────────────────────────────────
    print("  BMP180 — PRESSURE + TEMPERATURE")
    bmp = BMP180(address=0x77, simulate=args.simulate)
    readings = []
    for i in range(5):
        d = bmp.read()
        readings.append(d)
        t = d.get("temp_c")
        p = d.get("pressure_hpa")
        print(f"  Reading {i+1}/5:  T={t}°C   P={p} hPa   humidity=N/A (BMP180 has none)")
        time.sleep(0.5)

    temps = [r["temp_c"]       for r in readings if r["temp_c"]       is not None]
    press = [r["pressure_hpa"] for r in readings if r["pressure_hpa"] is not None]

    if temps:
        m, spread = sum(temps)/len(temps), max(temps)-min(temps)
        results.append(chk("Temperature plausible", 0 < m < 60,
                            f"{m:.1f}°C"))
        results.append(chk("Temperature stable",   spread < 2.0,
                            f"spread {spread:.2f}°C over 5 readings"))
    else:
        results.append(chk("BMP180 temperature", False, "no readings"))

    if press:
        m, spread = sum(press)/len(press), max(press)-min(press)
        results.append(chk("Pressure plausible", 800 < m < 1100,
                            f"{m:.1f} hPa"))
        results.append(chk("Pressure stable",   spread < 1.0,
                            f"spread {spread:.2f} hPa"))
    else:
        results.append(chk("BMP180 pressure", False, "no readings"))

    # ── DS3231 ───────────────────────────────────────────────
    print("\n  DS3231 — REAL TIME CLOCK")
    rtc = DS3231(simulate=args.simulate)

    if args.sync_rtc:
        print("  Syncing RTC from system clock...")
        rtc.sync_from_system()

    d = rtc.read()
    print(f"  RTC time:  {d['year']}-{d['month']:02d}-{d['day']:02d} "
          f"{d['hour']:02d}:{d['minute']:02d}:{d['second']:02d} UTC")
    print(f"  RTC temp:  {d['temperature_c']}°C")
    print(f"  Lost power: {d['lost_power']}")

    now_sys = datetime.now(timezone.utc)
    rtc_dt  = datetime(d["year"], d["month"], d["day"],
                       d["hour"], d["minute"], d["second"],
                       tzinfo=timezone.utc)
    drift_s = abs((rtc_dt - now_sys).total_seconds())

    results.append(chk("RTC time valid",     d["year"] >= 2024,
                        f"{d['year']}-{d['month']:02d}-{d['day']:02d}"))
    results.append(chk("RTC drift < 60s",   drift_s < 60,
                        f"{drift_s:.0f}s drift  "
                        f"{'OK' if drift_s < 5 else '→ run with --sync-rtc to fix'}"))
    results.append(chk("No lost-power flag", not d["lost_power"],
                        "OK" if not d["lost_power"] else
                        "Battery may be dead — replace CR2032"))

    # ── Cross-check temperatures ──────────────────────────────
    if temps:
        diff = abs(sum(temps)/len(temps) - d["temperature_c"])
        results.append(chk("Temp agreement BMP180 vs DS3231", diff < 8,
                            f"BMP={sum(temps)/len(temps):.1f}°C  "
                            f"DS3231={d['temperature_c']}°C  diff={diff:.1f}°C"))

    # ── Summary ──────────────────────────────────────────────
    n_pass = sum(results)
    n_fail = len(results) - n_pass
    print(f"\n  {'─'*52}")
    print(f"  {n_pass}/{len(results)} passed")
    if n_fail == 0:
        print("  \033[92m✓ I²C OK. Wire MCP3208 and run Test 3.\033[0m")
    else:
        print(f"  \033[91m✗ {n_fail} issue(s) — check wiring.\033[0m")
    if drift_s > 5 and not args.sync_rtc:
        print(f"  TIP: Re-run with --sync-rtc to set RTC from system time")
    print()

if __name__ == "__main__":
    main()
