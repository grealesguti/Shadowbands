#!/usr/bin/env python3
"""
recording/sky_snapshots.py
───────────────────────────
Capture periodic sky snapshots for post-eclipse cloud cover analysis.

Runs alongside the main sensor recording. Takes one JPEG every
interval_s seconds and saves with GPS-quality UTC timestamp in
the filename so frames can be correlated with sensor data.

Works with your 3.6mm IR camera — IR cameras see clouds well
(water droplets scatter IR just like visible light).

Usage:
  python3 recording/sky_snapshots.py                    # default 30s interval
  python3 recording/sky_snapshots.py --interval 60      # every 60 seconds
  python3 recording/sky_snapshots.py --interval 10      # every 10s near totality
  python3 recording/sky_snapshots.py --duration 3600    # run for 1 hour
  python3 recording/sky_snapshots.py --simulate         # no camera needed

Run in tmux alongside record_eclipse.py:
  tmux new -s eclipse
  python3 recording/record_eclipse.py &
  python3 recording/sky_snapshots.py
"""

import sys, time, subprocess, argparse, json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging

def capture_still(out_path, width=1280, height=720, stack="libcamera"):
    """Capture a single JPEG. Returns True on success."""
    if stack == "libcamera":
        cmd = ["libcamera-still",
               "-o", str(out_path),
               "--width",    str(width),
               "--height",   str(height),
               "--timeout",  "2000",
               "--nopreview",
               "--immediate",
               "--quality",  "85"]
    else:
        cmd = ["raspistill",
               "-o", str(out_path),
               "-w", str(width),
               "-h", str(height),
               "-t", "2000",
               "-n",
               "-q", "85"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10)
        return r.returncode == 0 and out_path.exists()
    except Exception:
        return False

def detect_stack():
    """Detect available camera stack."""
    r = subprocess.run(["which", "libcamera-still"],
                       capture_output=True, timeout=3)
    if r.returncode == 0:
        return "libcamera"
    r = subprocess.run(["which", "raspistill"],
                       capture_output=True, timeout=3)
    if r.returncode == 0:
        return "legacy"
    return None

def main():
    parser = argparse.ArgumentParser(description="Sky snapshot recorder")
    parser.add_argument("--interval",  type=int, default=30,
                        help="Seconds between snapshots (default 30)")
    parser.add_argument("--duration",  type=int, default=7200,
                        help="Total run time in seconds (default 7200 = 2h)")
    parser.add_argument("--width",     type=int, default=1280)
    parser.add_argument("--height",    type=int, default=720)
    parser.add_argument("--simulate",  action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    log = setup_logging(cfg)
    out_dir = Path(cfg["recording"]["output_dir"]) / "sky"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("\n╔══════════════════════════════════════════════════╗")
    print("║   Sky Snapshot Recorder                        ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"\n  Interval:   {args.interval}s")
    print(f"  Duration:   {args.duration}s ({args.duration/3600:.1f}h)")
    print(f"  Resolution: {args.width}×{args.height}")
    print(f"  Output:     {out_dir}/")

    # Estimate storage
    kb_per_frame = args.width * args.height * 0.05 / 1024  # ~5% JPEG ratio
    n_frames = args.duration // args.interval
    total_mb = kb_per_frame * n_frames / 1024
    print(f"  Frames:     ~{n_frames}")
    print(f"  Est. size:  ~{total_mb:.0f} MB\n")

    if args.simulate:
        log.info("Simulation mode — no actual captures")

    stack = detect_stack() if not args.simulate else "simulate"
    if stack is None:
        print("  ERROR: No camera stack found.")
        print("  Run: sudo raspi-config nonint do_camera 0")
        print("  Then reboot and try again.")
        return

    print(f"  Camera stack: {stack}")
    print(f"  Starting in 3 seconds... (Ctrl+C to stop)\n")
    time.sleep(3)

    # Index file — maps frame number to UTC timestamp
    index_path = out_dir / "index.json"
    index = []

    t0          = time.monotonic()
    n_captured  = 0
    n_failed    = 0
    next_capture = time.monotonic()

    try:
        while time.monotonic() - t0 < args.duration:

            now = time.monotonic()
            if now < next_capture:
                time.sleep(0.1)
                continue

            next_capture += args.interval
            utc_now = datetime.now(timezone.utc)
            ts_str  = utc_now.strftime("%Y%m%d_%H%M%S")
            ts_ns   = time.time_ns()

            frame_path = out_dir / f"sky_{ts_str}.jpg"

            if args.simulate:
                # Create a dummy file in simulation
                frame_path.write_text("simulated frame")
                ok = True
            else:
                ok = capture_still(frame_path, args.width,
                                   args.height, stack)

            elapsed = time.monotonic() - t0
            remaining = args.duration - elapsed

            if ok:
                n_captured += 1
                size_kb = frame_path.stat().st_size / 1024
                index.append({
                    "frame": n_captured,
                    "ts_ns": ts_ns,
                    "utc":   utc_now.isoformat(),
                    "file":  frame_path.name,
                    "kb":    round(size_kb, 1),
                })
                # Save index after every frame
                with open(index_path, "w") as f:
                    json.dump(index, f, indent=2)
                print(f"  [{n_captured:>4}] {utc_now.strftime('%H:%M:%S')} UTC  "
                      f"{size_kb:>6.0f} KB  "
                      f"remaining: {remaining/60:.0f}min")
            else:
                n_failed += 1
                log.warning("Capture failed at %s", utc_now.isoformat())
                print(f"  [{n_captured:>4}] {utc_now.strftime('%H:%M:%S')} UTC  "
                      f"FAILED ({n_failed} total failures)")

    except KeyboardInterrupt:
        print("\n  Stopped by user.")

    print(f"\n  {'─'*50}")
    print(f"  Captured:  {n_captured} frames")
    print(f"  Failed:    {n_failed} frames")
    print(f"  Index:     {index_path}")
    print(f"  Output:    {out_dir}/")
    if n_captured > 0:
        total_kb = sum(f["kb"] for f in index)
        print(f"  Total size: {total_kb/1024:.1f} MB")
    print()

if __name__ == "__main__":
    main()
