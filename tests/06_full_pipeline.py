#!/usr/bin/env python3
"""
tests/06_full_pipeline.py
──────────────────────────
Test 6 — All sensors simultaneously for 60 seconds.

All hardware from tests 2–5 must be connected:
  BMP180  → I²C (SDA pin 3, SCL pin 5)
  DS3231  → I²C (same bus)
  MCP3208 → SPI (CE0 pin 24)
  OPT101 ×2 → MCP3208 CH0 + CH1
  ADXL335   → MCP3208 CH5 + CH6 + CH7

Usage: python3 tests/06_full_pipeline.py
       python3 tests/06_full_pipeline.py --duration 120
       python3 tests/06_full_pipeline.py --simulate
"""
import sys, csv, time, threading, argparse, statistics, os
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, Hardware

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<40} {detail}")
    return ok

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()

    cfg  = load_config()
    log  = setup_logging(cfg)
    out_dir = Path(cfg["recording"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 6 — Full Pipeline                        ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"\n  Recording {args.duration}s → {out_dir}/\n")

    hw = Hardware(cfg=cfg, simulate=args.simulate)

    stamp    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    adc_path = out_dir / f"pipeline_adc_{stamp}.csv"
    atm_path = out_dir / f"pipeline_atmos_{stamp}.csv"

    buf    = cfg["recording"]["write_buffer_kb"] * 1024
    adc_fh = open(adc_path, "w", newline="", buffering=buf)
    atm_fh = open(atm_path, "w", newline="", buffering=buf)
    adc_wr = csv.writer(adc_fh)
    atm_wr = csv.writer(atm_fh)
    lock   = threading.Lock()

    n_ch = cfg["hardware"]["n_opt101"]
    adc_wr.writerow(["ts_ns"] +
                    [f"opt{c}_raw" for c in range(n_ch)] +
                    [f"opt{c}_cal" for c in range(n_ch)] +
                    ["accel_x_raw","accel_y_raw","accel_z_raw"])
    atm_wr.writerow(["ts_ns","temp_c","pressure_hpa",
                     "rtc_temp_c","accel_x_g","accel_y_g","accel_z_g"])

    stop   = threading.Event()
    stats  = {"adc": 0, "atmos": 0, "errors": 0, "last_ts": 0, "ts_err": 0}
    target = cfg["recording"]["adc_target_sps"]

    def adc_thread():
        interval = 1.0 / target
        next_t   = time.monotonic()
        while not stop.is_set():
            sleep_s = next_t - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)
            next_t += interval
            try:
                ts   = time.time_ns()
                raws = hw.read_all_raw()
                cals = hw.read_all_cal()
                acc  = hw.accel.read_raw()
                row  = ([ts] + raws + [round(v,2) for v in cals] +
                        [acc["x"], acc["y"], acc["z"]])
                with lock:
                    adc_wr.writerow(row)
                    if stats["last_ts"] and ts <= stats["last_ts"]:
                        stats["ts_err"] += 1
                    stats["last_ts"] = ts
                stats["adc"] += 1
            except Exception as e:
                stats["errors"] += 1

    def atmos_thread():
        while not stop.is_set():
            try:
                ts  = time.time_ns()
                atm = hw.read_atmosphere()
                rtc = hw.rtc.read()
                acc = hw.accel.read()
                with lock:
                    atm_wr.writerow([
                        ts,
                        atm.get("temp_c"), atm.get("pressure_hpa"),
                        rtc.get("temperature_c"),
                        acc["x"], acc["y"], acc["z"],
                    ])
                stats["atmos"] += 1
            except Exception as e:
                stats["errors"] += 1
            time.sleep(1.0)

    def status_thread():
        t0 = time.monotonic()
        while not stop.is_set():
            el  = time.monotonic() - t0
            sps = stats["adc"] / max(el, 1)
            sys.stdout.write(
                f"\r  {el:>5.1f}/{args.duration}s  "
                f"ADC:{stats['adc']:>7,}  sps:{sps:>5.0f}  "
                f"atmos:{stats['atmos']:>4}  err:{stats['errors']}"
            )
            sys.stdout.flush()
            time.sleep(0.5)

    threads = [
        threading.Thread(target=adc_thread,    daemon=True),
        threading.Thread(target=atmos_thread,  daemon=True),
        threading.Thread(target=status_thread, daemon=True),
    ]
    for t in threads: t.start()

    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        print("\n  Interrupted")

    stop.set()
    adc_fh.flush(); adc_fh.close()
    atm_fh.flush(); atm_fh.close()
    hw.close()

    elapsed    = args.duration
    actual_sps = stats["adc"] / elapsed
    adc_kb     = adc_path.stat().st_size / 1024
    atm_kb     = atm_path.stat().st_size / 1024

    print(f"\n\n  {'─'*52}")
    results = []
    results.append(chk("ADC ≥ 100 sps",           actual_sps >= 100,
                        f"{actual_sps:.0f} sps"))
    results.append(chk("Atmos samples complete",   stats["atmos"] >= elapsed - 3,
                        f"{stats['atmos']} rows"))
    results.append(chk("No errors",                stats["errors"] == 0,
                        f"{stats['errors']} errors"))
    results.append(chk("Timestamps monotonic",     stats["ts_err"] == 0,
                        f"{stats['ts_err']} inversions"))
    results.append(chk("ADC file written",         adc_kb > 5,
                        f"{adc_kb:.0f} KB"))
    results.append(chk("Atmos file written",       atm_kb > 0.5,
                        f"{atm_kb:.1f} KB"))

    # CSV integrity check
    try:
        with open(adc_path) as f:
            lines = f.readlines()
        ts_vals = [int(l.split(",")[0]) for l in lines[1:201] if l.strip()]
        mono = all(ts_vals[i] < ts_vals[i+1] for i in range(len(ts_vals)-1))
        results.append(chk("CSV timestamps monotonic", mono,
                            f"checked {len(ts_vals)} rows"))
    except Exception as e:
        results.append(chk("CSV check", False, str(e)))

    n_pass = sum(results)
    n_fail = len(results) - n_pass
    print(f"\n  Files:")
    print(f"    {adc_path.name}  ({adc_kb:.0f} KB)")
    print(f"    {atm_path.name}  ({atm_kb:.1f} KB)")
    print(f"\n  {n_pass}/{len(results)} passed")
    if n_fail == 0:
        print("  \033[92m✓ Full pipeline working.\033[0m")
        print("  \033[92m  Next: run calibration, then buy 6× OPT101P + MCP3208 #2.\033[0m")
    else:
        print(f"  \033[91m✗ {n_fail} issue(s) — see above.\033[0m")
    print()

if __name__ == "__main__":
    main()
