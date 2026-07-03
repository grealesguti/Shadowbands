#!/usr/bin/env python3
"""
tests/08_camera_and_sensors.py
───────────────────────────────
Test 8 — Camera + all sensors simultaneously.

This is the final integration test before eclipse deployment.
Runs the full sensor pipeline AND camera recording in parallel
for 30 seconds, verifying they don't interfere with each other
on the Pi Zero W's single core.

This matters because on Pi Zero W:
  - Camera H264 encoding uses the GPU (VideoCore) — separate from CPU
  - Sensor logging uses CPU in Python threads
  - They should coexist, but this test confirms it

Usage:
  python3 tests/08_camera_and_sensors.py
  python3 tests/08_camera_and_sensors.py --duration 60
  python3 tests/08_camera_and_sensors.py --simulate  (no hardware)
"""

import sys, csv, time, threading, subprocess, argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, Hardware

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<42} {detail}")
    return ok

def start_camera(vid_path, width=1280, height=720, fps=30, duration_s=30):
    """Start camera recording in a subprocess. Returns the process."""
    cmd = ["libcamera-vid",
           "-o", str(vid_path),
           "--width",     str(width),
           "--height",    str(height),
           "--framerate", str(fps),
           "--timeout",   str(duration_s * 1000),
           "--nopreview",
           "--codec",     "h264"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        return proc
    except FileNotFoundError:
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()

    cfg  = load_config()
    log  = setup_logging(cfg)
    out_dir = Path(cfg["recording"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 8 — Camera + Sensors Simultaneous        ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"\n  Running {args.duration}s with camera + all sensors\n")

    hw = Hardware(cfg=cfg, simulate=args.simulate)

    # ── Start camera first ────────────────────────────────────
    vid_path = out_dir / f"combined_video_{stamp}.h264"
    cam_cfg  = cfg["hardware"]

    if not args.simulate:
        print("  Starting camera recording...")
        cam_proc = start_camera(vid_path,
                                width=1280, height=720, fps=30,
                                duration_s=args.duration + 5)
        if cam_proc is None:
            print("  Camera not available (libcamera not found)")
            print("  Run test 7 first, then re-run this test")
            cam_proc = None
        else:
            time.sleep(2)  # let camera initialise
            cam_running = cam_proc.poll() is None
            print(f"  Camera process: {'running ✓' if cam_running else 'failed ✗'}")
    else:
        cam_proc = None
        print("  Camera: simulation mode (skipped)")

    # ── Sensor logging ────────────────────────────────────────
    adc_path = out_dir / f"combined_adc_{stamp}.csv"
    atm_path = out_dir / f"combined_atmos_{stamp}.csv"

    adc_fh = open(adc_path, "w", newline="", buffering=65536)
    atm_fh = open(atm_path, "w", newline="", buffering=65536)
    adc_wr = csv.writer(adc_fh)
    atm_wr = csv.writer(atm_fh)
    lock   = threading.Lock()
    n_ch   = cfg["hardware"]["n_opt101"]

    adc_wr.writerow(["ts_ns"] + [f"opt{c}" for c in range(n_ch)] +
                    ["ax_raw","ay_raw","az_raw"])
    atm_wr.writerow(["ts_ns","temp_c","pressure_hpa","ax_g","ay_g","az_g"])

    stop  = threading.Event()
    stats = {"adc": 0, "atmos": 0, "errors": 0}
    target = cfg["recording"]["adc_target_sps"]

    def adc_thread():
        interval = 1.0 / target
        next_t   = time.monotonic()
        while not stop.is_set():
            sl = next_t - time.monotonic()
            if sl > 0: time.sleep(sl)
            next_t += interval
            try:
                ts   = time.time_ns()
                raws = hw.read_all_raw()
                acc  = hw.accel.read_raw()
                with lock:
                    adc_wr.writerow([ts] + raws + [acc["x"],acc["y"],acc["z"]])
                stats["adc"] += 1
            except Exception:
                stats["errors"] += 1

    def atmos_thread():
        while not stop.is_set():
            try:
                ts  = time.time_ns()
                atm = hw.bmp.read()
                acc = hw.accel.read()
                with lock:
                    atm_wr.writerow([ts, atm.get("temp_c"),
                                     atm.get("pressure_hpa"),
                                     acc["x"], acc["y"], acc["z"]])
                stats["atmos"] += 1
            except Exception:
                stats["errors"] += 1
            time.sleep(1.0)

    def status_thread():
        t0 = time.monotonic()
        while not stop.is_set():
            el  = time.monotonic() - t0
            sps = stats["adc"] / max(el, 1)
            cam_ok = cam_proc and cam_proc.poll() is None
            sys.stdout.write(
                f"\r  {el:>4.0f}/{args.duration}s  "
                f"ADC:{stats['adc']:>6,}  sps:{sps:>4.0f}  "
                f"atmos:{stats['atmos']:>3}  "
                f"cam:{'▶ recording' if cam_ok else '■ stopped'}"
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

    # Stop camera cleanly
    if cam_proc:
        cam_proc.terminate()
        cam_proc.wait(timeout=5)

    hw.close()

    actual_sps = stats["adc"] / args.duration
    adc_kb = adc_path.stat().st_size / 1024
    vid_kb = vid_path.stat().st_size / 1024 if vid_path.exists() else 0

    print(f"\n\n  {'─'*52}")
    results = []
    results.append(chk("ADC sps with camera running", actual_sps >= 80,
                        f"{actual_sps:.0f} sps  "
                        f"({'OK' if actual_sps >= 80 else 'too slow — lower target_sps'})"))
    results.append(chk("No sensor errors",            stats["errors"] == 0,
                        f"{stats['errors']} errors"))
    results.append(chk("ADC data written",             adc_kb > 1,
                        f"{adc_kb:.0f} KB"))
    results.append(chk("Atmos data written",           stats["atmos"] >= args.duration - 3,
                        f"{stats['atmos']} rows"))

    if cam_proc is not None:
        results.append(chk("Video file written",       vid_kb > 100,
                            f"{vid_kb:.0f} KB → {vid_path.name}"))
        if actual_sps < 80:
            print("\n  TIP: Camera + sensors on Pi Zero W is tight.")
            print("  Try lowering adc_target_sps to 100 in config.yaml")

    n_pass = sum(results)
    n_fail = len(results) - n_pass
    print(f"\n  {n_pass}/{len(results)} passed")
    if n_fail == 0:
        print("  \033[92m✓ Camera + sensors working together.\033[0m")
        print("  \033[92m  System is ready for eclipse deployment.\033[0m")
    else:
        print(f"  \033[91m✗ {n_fail} issue(s).\033[0m")
    print()

if __name__ == "__main__":
    main()
