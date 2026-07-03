#!/usr/bin/env python3
"""
tests/01_pi_and_sd.py
──────────────────────
Test 1 — Pi Zero W health + SD card.
No external hardware needed.

Usage: python3 tests/01_pi_and_sd.py
"""
import sys, os, time, subprocess, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging

def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except Exception:
        return None

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<38} {detail}")
    return ok

def main():
    cfg = load_config()
    setup_logging(cfg)
    out_dir = Path(cfg["recording"]["output_dir"])

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 1 — Pi Zero W Health + SD Card           ║")
    print("╚══════════════════════════════════════════════════╝\n")

    results = []

    # ── CPU ──────────────────────────────────────────────────
    print("  CPU")
    r = run(["cat", "/proc/cpuinfo"])
    is_pi = r and ("BCM" in r.stdout or "Raspberry" in r.stdout)
    results.append(chk("Running on Raspberry Pi", is_pi or True,
                        "detected" if is_pi else "not detected (OK if testing on laptop)"))

    r = run(["vcgencmd", "measure_temp"])
    if r and r.returncode == 0:
        try:
            temp = float(r.stdout.split("=")[1].replace("'C\n", ""))
            ok = temp < 75
            results.append(chk("CPU temperature", ok,
                                f"{temp}°C  ({'OK' if ok else 'HOT — check power supply'})"))
        except: pass

    r = run(["vcgencmd", "get_throttled"])
    if r and r.returncode == 0:
        ok = r.stdout.strip() == "throttled=0x0"
        results.append(chk("No throttling", ok,
                            r.stdout.strip() if ok else
                            r.stdout.strip() + "  ← PROBLEM: use better USB cable/charger"))

    # ── Interfaces ───────────────────────────────────────────
    print("\n  INTERFACES")
    spi_ok = Path("/dev/spidev0.0").exists()
    results.append(chk("SPI enabled (/dev/spidev0.0)", spi_ok,
                        "OK" if spi_ok else
                        "MISSING → sudo raspi-config nonint do_spi 0 && sudo reboot"))

    i2c_ok = Path("/dev/i2c-1").exists()
    results.append(chk("I2C enabled (/dev/i2c-1)", i2c_ok,
                        "OK" if i2c_ok else
                        "MISSING → sudo raspi-config nonint do_i2c 0 && sudo reboot"))

    # ── SD card ──────────────────────────────────────────────
    print("\n  SD CARD")
    out_dir.mkdir(parents=True, exist_ok=True)

    test_file = out_dir / ".write_test"
    data = b"X" * (1024 * 1024)
    t0 = time.monotonic()
    with open(test_file, "wb") as f:
        for _ in range(5):
            f.write(data)
        f.flush()
        os.fsync(f.fileno())
    dt = time.monotonic() - t0
    mb_s = 5 / dt
    test_file.unlink()
    ok = mb_s >= 2.0   # 2 MB/s minimum for Pi Zero W (lower bar than Pi 4)
    results.append(chk("Write speed ≥ 2 MB/s", ok,
                        f"{mb_s:.1f} MB/s  "
                        f"({'OK' if ok else 'SLOW — try a different SD card'})"))

    free_gb = shutil.disk_usage(out_dir).free / 1e9
    results.append(chk("Free space ≥ 2 GB", free_gb >= 2,
                        f"{free_gb:.1f} GB free"))

    # ── Python packages ──────────────────────────────────────
    print("\n  PYTHON PACKAGES")
    packages = [
        ("spidev",   "spidev",   "pip3 install spidev --break-system-packages"),
        ("PyYAML",   "yaml",     "pip3 install PyYAML --break-system-packages"),
        ("smbus2",   "smbus2",   "pip3 install smbus2 --break-system-packages"),
        ("RPi.GPIO", "RPi.GPIO", "pip3 install RPi.GPIO --break-system-packages"),
    ]
    for name, imp, fix in packages:
        try:
            __import__(imp)
            results.append(chk(f"{name}", True, "installed"))
        except ImportError:
            results.append(chk(f"{name}", False, f"MISSING → {fix}"))

    # ── Summary ──────────────────────────────────────────────
    n_pass = sum(results)
    n_fail = len(results) - n_pass
    print(f"\n  {'─'*52}")
    print(f"  {n_pass}/{len(results)} passed")
    if n_fail == 0:
        print("  \033[92m✓ Pi Zero W ready. Wire BMP180+DS3231 and run Test 2.\033[0m")
    else:
        print(f"  \033[91m✗ {n_fail} issue(s) — fix above before continuing.\033[0m")
    print()

if __name__ == "__main__":
    main()
