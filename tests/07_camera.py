#!/usr/bin/env python3
"""
tests/07_camera.py
───────────────────
Test 7 — OV5647 IR Camera via rpicam stack.

Confirmed working setup:
  Camera:    OV5647 5MP IR 1080p, 3.6mm lens
  Commands:  rpicam-still / rpicam-vid  (NOT libcamera-* on Trixie)
  Detection: rpicam-hello --list-cameras  (NOT vcgencmd get_camera)
  I2C:       Address 0x36 shows as UU on bus 10 — this is CORRECT

Prerequisites:
  /boot/firmware/config.txt must contain (in [all] section):
    camera_auto_detect=0
    start_x=1
    gpu_mem=128
    dtoverlay=ov5647

Usage:
  python3 tests/07_camera.py
  python3 tests/07_camera.py --no-video
  python3 tests/07_camera.py --fps 60
"""

import sys, os, subprocess, time, argparse, getpass, socket
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, detect_camera_stack, \
                     camera_is_present, start_video


def capture_still_direct(path, width, height, stack="rpicam", timeout_ms=2000):
    """
    Self-contained still capture via subprocess — same proven pattern as
    the video section. Does not depend on utils.hw.capture_still, and does
    not suppress stderr, so real errors are visible on failure.
    Returns True if the file exists and is non-trivial.
    """
    cmd = [f"{stack}-still",
           "-o", str(path),
           "--width",  str(width),
           "--height", str(height),
           "-t",       str(timeout_ms),
           "--nopreview"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print("  (capture timed out after 60 s)")
        return False
    ok = Path(path).exists() and Path(path).stat().st_size > 10000
    if not ok and r.stderr:
        # show the tail of stderr so failures are never silent again
        print("  stderr: " + r.stderr.strip().splitlines()[-1][:120])
    return ok

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<42} {detail}")
    return ok

def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True,
                              text=True, timeout=timeout)
    except Exception:
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-video",  action="store_true")
    parser.add_argument("--fps",       type=int, default=30)
    parser.add_argument("--width",     type=int, default=1920)
    parser.add_argument("--height",    type=int, default=1080)
    args = parser.parse_args()

    cfg    = load_config()
    setup_logging(cfg)
    hcfg   = cfg["hardware"]
    # Robust output dir: expand ~, and if the configured path is not
    # writable (e.g. config.yaml hardcodes another user's home), fall
    # back to ~/data so the test never fails on paths.
    out_dir = Path(os.path.expanduser(cfg["recording"]["output_dir"]))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".write_test"
        probe.touch(); probe.unlink()
    except (PermissionError, OSError):
        fallback = Path.home() / "data"
        print(f"  WARNING: {out_dir} not writable — using {fallback}")
        print(f"           fix config.yaml paths (bash install.sh)")
        out_dir = fallback
        out_dir.mkdir(parents=True, exist_ok=True)
    stamp  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 7 — OV5647 IR Camera                     ║")
    print("║  Using rpicam-* stack (Raspbian Trixie)        ║")
    print("╚══════════════════════════════════════════════════╝\n")

    results = []

    # ── Stack detection ───────────────────────────────────────
    print("  CAMERA STACK")
    stack = detect_camera_stack()
    if stack:
        results.append(chk(f"{stack}-* commands found", True,
                            f"using {stack}-still and {stack}-vid"))
    else:
        results.append(chk("Camera commands", False,
                            "rpicam-still not found"))
        print("\n  Fix:")
        print("  sudo apt install -y rpicam-apps")
        return

    print(f"\n  NOTE: vcgencmd get_camera shows detected=0 on Trixie")
    print(f"        even when working — this is NORMAL. Ignore it.")
    print(f"        Use rpicam-hello --list-cameras instead.\n")

    # ── Camera detection ──────────────────────────────────────
    print("  CAMERA DETECTION")
    r = run([f"{stack}-hello", "--list-cameras"], timeout=10)
    if r and r.returncode == 0 and "ov5647" in r.stdout.lower():
        # Print camera info
        for line in r.stdout.strip().split("\n")[:5]:
            if line.strip():
                print(f"  {line}")
        results.append(chk("OV5647 detected", True,
                            "found on /base/soc/i2c0mux/i2c@1/ov5647@36"))
    elif r and "Available cameras" in r.stdout:
        print(r.stdout[:200])
        results.append(chk("Camera detected", True, "found (non-OV5647)"))
    else:
        results.append(chk("Camera detected", False,
                            "not found — check cable and /boot/firmware/config.txt"))
        print("\n  Required in /boot/firmware/config.txt [all] section:")
        print("    camera_auto_detect=0")
        print("    start_x=1")
        print("    gpu_mem=128")
        print("    dtoverlay=ov5647")
        print("\n  Also verify: sudo i2cdetect -y 10")
        print("  Should show UU at address 0x36")
        return

    # ── I2C verification ──────────────────────────────────────
    print("\n  I²C VERIFICATION")
    r = run(["sudo", "i2cdetect", "-y", "10"])
    if r and "UU" in r.stdout:
        results.append(chk("OV5647 on I²C bus 10 @ 0x36", True,
                            "UU = kernel driver active = correct"))
    else:
        results.append(chk("OV5647 I²C check", False,
                            "0x36 not found on bus 10"))

    # ── Still image ───────────────────────────────────────────
    print("\n  STILL IMAGE")
    img_path = out_dir / f"cam_test_{stamp}.jpg"
    print(f"  Capturing {args.width}×{args.height} JPEG...")

    ok = capture_still_direct(img_path, args.width, args.height, stack=stack)
    if ok:
        size_kb = img_path.stat().st_size / 1024
        results.append(chk("Still image captured", True,
                            f"{size_kb:.0f} KB → {img_path.name}"))
        user, host = getpass.getuser(), socket.gethostname()
        print(f"  Saved: {img_path}")
        print(f"  Copy to laptop: scp {user}@{host}.local:"
              f"{img_path} /mnt/c/Users/Guillermo/Downloads/")
    else:
        results.append(chk("Still image captured", False,
                            "file not created — check camera connection"))

    # ── Video recording ───────────────────────────────────────
    if not args.no_video:
        print(f"\n  VIDEO RECORDING (5s, {args.width}×{args.height} @ {args.fps}fps)")
        vid_path = out_dir / f"cam_video_{stamp}.h264"

        cmd = [f"{stack}-vid",
               "-o", str(vid_path),
               "--width",     str(args.width),
               "--height",    str(args.height),
               "--framerate", str(args.fps),
               "--timeout",   "5000",
               "--nopreview",
               "--codec",     "h264"]

        print(f"  Recording 5 seconds...")
        r = run(cmd, timeout=15)
        if vid_path.exists() and vid_path.stat().st_size > 10000:
            size_kb = vid_path.stat().st_size / 1024
            results.append(chk("5s video recorded", True,
                                f"{size_kb:.0f} KB → {vid_path.name}"))
        else:
            stderr = r.stderr[:100] if r and r.stderr else "no output"
            results.append(chk("5s video recorded", False, stderr))

    # ── Sky snapshot test ─────────────────────────────────────
    print("\n  SKY SNAPSHOT TEST")
    print("  Point camera at sky or window...")
    sky_path = out_dir / f"sky_test_{stamp}.jpg"
    ok = capture_still_direct(sky_path, 1280, 720, stack=stack)
    if ok:
        size_kb = sky_path.stat().st_size / 1024
        results.append(chk("Sky snapshot", True,
                            f"{size_kb:.0f} KB — copy to laptop to check framing"))
    else:
        results.append(chk("Sky snapshot", False, "capture failed"))

    # ── Summary ──────────────────────────────────────────────
    n_pass = sum(results)
    n_fail = len(results) - n_pass
    print(f"\n  {'─'*52}")
    print(f"  {n_pass}/{len(results)} passed")

    if n_fail == 0:
        print("  \033[92m✓ Camera fully working.\033[0m")
        print("  \033[92m  Run Test 8 to verify camera + sensors together.\033[0m")
    else:
        print(f"  \033[91m✗ {n_fail} issue(s).\033[0m")
        print("\n  Troubleshooting:")
        print("  1. Check /boot/firmware/config.txt has camera_auto_detect=0")
        print("     and dtoverlay=ov5647 in [all] section")
        print("  2. sudo i2cdetect -y 10 — should show UU at 0x36")
        print("  3. Reseat flat cable with Pi powered off")
        print("  4. For Pi Zero W: need 15-to-22-pin adapter cable")
    print()

if __name__ == "__main__":
    main()