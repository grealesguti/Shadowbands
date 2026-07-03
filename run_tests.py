#!/usr/bin/env python3
"""
run_tests.py — Interactive test menu
Usage:
  python3 run_tests.py           # menu
  python3 run_tests.py 1         # run test 1
  python3 run_tests.py all       # run all
"""
import sys, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = [
    ("01 — Pi Zero W health + SD card",          "tests/01_pi_and_sd.py"),
    ("02 — I²C bus + BMP180 + DS3231",           "tests/02_i2c_bmp180_ds3231.py"),
    ("03 — SPI bus + MCP3208 ADC",               "tests/03_spi_mcp3208.py"),
    ("04 — OPT101 × 2 photodiodes",              "tests/04_opt101_two_sensors.py"),
    ("05 — ADXL335 accelerometer",               "tests/05_adxl335.py"),
    ("06 — Full pipeline (60s recording)",        "tests/06_full_pipeline.py"),
    ("07 — Camera OV5647 (rpicam stack)",         "tests/07_camera.py"),
    ("08 — Camera + sensors simultaneous",        "tests/08_camera_and_sensors.py"),
]

def run(path, extra=[]):
    subprocess.run([sys.executable, str(ROOT/path)] + extra)

def menu():
    print("\n╔══════════════════════════════════════════════════╗")
    print("║   Shadow Band Detector — Test Suite  v5        ║")
    print("║   Pi Zero W · eclipse@eclipse.local            ║")
    print("║   Camera: OV5647 IR · rpicam stack             ║")
    print("╚══════════════════════════════════════════════════╝\n")
    for i,(label,_) in enumerate(TESTS,1):
        print(f"  [{i}]  {label}")
    print("  [s]  Start sky snapshots (recording/sky_snapshots.py)")
    print("  [a]  Run all tests in sequence")
    print("  [q]  Quit\n")
    return input("  Select: ").strip().lower()

def main():
    args = sys.argv[1:]
    if args:
        if args[0] == "all":
            for _,p in TESTS: run(p)
            return
        if args[0] == "s":
            run("recording/sky_snapshots.py", args[1:])
            return
        try:
            idx = int(args[0]) - 1
            if 0 <= idx < len(TESTS):
                run(TESTS[idx][1], args[1:])
            return
        except ValueError:
            pass

    while True:
        choice = menu()
        if choice == "q": break
        elif choice == "a":
            for _,p in TESTS:
                run(p)
                input("\n  Press Enter for next test...")
        elif choice == "s":
            run("recording/sky_snapshots.py")
            input("\n  Press Enter to return to menu...")
        elif choice.isdigit() and 1 <= int(choice) <= len(TESTS):
            run(TESTS[int(choice)-1][1])
            input("\n  Press Enter to return to menu...")
        else:
            print("  Invalid.")

if __name__ == "__main__":
    main()
