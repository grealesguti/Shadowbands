#!/usr/bin/env python3
"""
tests/04_opt101_two_sensors.py
───────────────────────────────
Test 4 — Both OPT101 photodiodes through MCP3208.

Wiring (keep MCP3208 from Test 3, remove voltage divider):

  OPT101 DIP-8 pinout (holding chip, notch at top):
    Pin 1  VOUT → MCP3208 CH0  (sensor #1)  or  CH1 (sensor #2)
    Pin 2  +Vs  → Pi 3.3V (pin 1)
    Pin 3  GND  → Pi GND  (pin 6)
    Pin 5  NC   → leave OPEN
    Pin 8  NC   → leave OPEN
    (pins 4, 6, 7 also NC)

  OPT101 #1: VOUT (pin 1) → MCP3208 CH0 (pin 1)
  OPT101 #2: VOUT (pin 1) → MCP3208 CH1 (pin 2)

  Decoupling (from your cap kit):
    10µF electrolytic per OPT101: +Vs (pin 2) to GND (pin 3)

  Your solar filter film (DIN EN ISO 12312-2) used in Test 3
  as a very strong ND attenuator — hold a small square over
  one sensor during the attenuation test.

Usage: python3 tests/04_opt101_two_sensors.py
       python3 tests/04_opt101_two_sensors.py --simulate
"""
import sys, time, csv, statistics, argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, MCP3208

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<42} {detail}")
    return ok

def collect(adc, channels, duration_s, sps=200):
    data, interval = [], 1.0 / sps
    t0, next_t = time.monotonic(), time.monotonic()
    while time.monotonic() - t0 < duration_s:
        now = time.monotonic()
        if now >= next_t:
            ts   = time.time_ns()
            vals = [adc.read(ch) for ch in channels]
            data.append((ts, vals))
            next_t += interval
    return data

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()

    cfg  = load_config()
    setup_logging(cfg)
    hcfg = cfg["hardware"]
    out_dir = Path(cfg["recording"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 4 — OPT101 × 2 Photodiodes              ║")
    print("╚══════════════════════════════════════════════════╝")
    print("\n  OPT101 #1 → CH0,   OPT101 #2 → CH1\n")

    adc     = MCP3208(bus=hcfg["spi_bus"], ce=hcfg["spi_ce_mcp3208_1"],
                      hz=hcfg["spi_hz"], simulate=args.simulate)
    results = []
    dark    = [0.0, 0.0]

    # ── Test 1: Dark ─────────────────────────────────────────
    print("  TEST 1 — DARK OFFSET")
    input("  Cover BOTH OPT101s completely (cap or dark cloth) → Enter ")
    time.sleep(0.5)
    dark_data = collect(adc, [0, 1], 3.0, 200)
    for i in range(2):
        vals   = [r[1][i] for r in dark_data]
        dark[i] = statistics.mean(vals)
        std    = statistics.stdev(vals)
        ok     = dark[i] < 100 and std < 8
        results.append(chk(f"OPT101 #{i+1} dark offset",
                            ok, f"{dark[i]:.1f} ± {std:.1f} counts"))

    # ── Test 2: Light response ────────────────────────────────
    print("\n  TEST 2 — LIGHT RESPONSE")
    input("  Uncover both (indoor diffuse light) → Enter ")
    time.sleep(0.5)
    light_data = collect(adc, [0, 1], 3.0, 200)
    light_ref  = []
    for i in range(2):
        vals = [r[1][i] for r in light_data]
        m    = statistics.mean(vals)
        s    = statistics.stdev(vals)
        light_ref.append(m)
        net  = m - dark[i]
        snr  = 20 * __import__("math").log10(net / (s + 1e-9)) if net > 0 else 0
        ok   = net > 50
        results.append(chk(f"OPT101 #{i+1} light response",
                            ok, f"net={net:.0f} counts  SNR={snr:.1f}dB"))

    # ── Test 3: Solar filter film ─────────────────────────────
    print("\n  TEST 3 — SOLAR FILTER FILM (your DIN EN ISO 12312-2)")
    print("  Hold a small piece over OPT101 #1 only.")
    input("  Apply solar filter over #1 → Enter ")
    time.sleep(0.5)
    filt0 = statistics.mean(adc.read(0) for _ in range(100)) - dark[0]
    filt1 = statistics.mean(adc.read(1) for _ in range(100)) - dark[1]
    ref0  = light_ref[0] - dark[0]
    pct   = filt0 / max(ref0, 1) * 100
    print(f"  #1 with filter: {filt0:.1f} net  ({pct:.3f}% of open signal)")
    print(f"  #2 open:        {filt1:.1f} net")
    results.append(chk("Solar film attenuates #1", filt0 < ref0 * 0.01,
                        f"reduced to {pct:.3f}% — very strong ND as expected"))

    # ── Test 4: Matching ──────────────────────────────────────
    print("\n  TEST 4 — SENSOR MATCHING")
    input("  Remove filter, both open in same light → Enter ")
    time.sleep(0.5)
    m0 = statistics.mean(adc.read(0) - dark[0] for _ in range(200))
    m1 = statistics.mean(adc.read(1) - dark[1] for _ in range(200))
    if m0 > 0 and m1 > 0:
        dev = abs(m0 - m1) / max(m0, m1) * 100
        ok  = dev < 25
        results.append(chk("Sensor matching < 25%", ok,
                            f"CH0={m0:.0f}  CH1={m1:.0f}  dev={dev:.1f}%"))
        if dev > 5:
            factor = m0 / m1
            print(f"  → Calibration will correct this. Factor CH1: {factor:.4f}")

    # ── Test 5: Shadow sweep ──────────────────────────────────
    print("\n  TEST 5 — SHADOW SWEEP (core shadow band simulation)")
    spacing = hcfg["sensor_spacing_m"]
    print(f"  Sensor spacing: {spacing*100:.1f} cm")
    print(f"  Sweep a 5cm matte-black card from #1 toward #2.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    sweep_path = out_dir / f"sweep_2sensor_{stamp}.csv"

    with open(sweep_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ns","ch0_raw","ch1_raw","ch0_net","ch1_net","trial"])

        for trial in range(3):
            label = ["slow ~0.3 m/s", "medium ~1 m/s", "fast ~3 m/s"][trial]
            input(f"\n  Trial {trial+1}: {label} → Enter then sweep ")

            data = collect(adc, [0, 1], 3.0, 400)
            for ts, vals in data:
                w.writerow([ts, vals[0], vals[1],
                             round(vals[0]-dark[0],1),
                             round(vals[1]-dark[1],1), trial+1])

            ch0 = [r[1][0]-dark[0] for r in data]
            ch1 = [r[1][1]-dark[1] for r in data]
            ts_arr = [r[0] for r in data]
            n = len(data)
            lo, hi = int(n*0.1), int(n*0.9)

            if hi > lo + 10:
                dip0 = lo + ch0[lo:hi].index(min(ch0[lo:hi]))
                dip1 = lo + ch1[lo:hi].index(min(ch1[lo:hi]))
                delay_ms = (ts_arr[dip1] - ts_arr[dip0]) / 1e6
                if abs(delay_ms) > 1:
                    v = spacing / (delay_ms / 1000)
                    direction = "→" if delay_ms > 0 else "←"
                    print(f"  Delay: {delay_ms:+.1f}ms  velocity: {abs(v):.2f} m/s {direction}")
                    results.append(chk(f"Trial {trial+1} velocity",
                                       0.1 < abs(v) < 20,
                                       f"{abs(v):.2f} m/s"))
                else:
                    print(f"  Dip simultaneous — sweep faster or increase spacing")

    print(f"\n  Sweep data → {sweep_path}")
    adc.close()

    n_pass = sum(results)
    n_fail = len(results) - n_pass
    print(f"\n  {'─'*52}")
    print(f"  {n_pass}/{len(results)} passed")
    if n_fail == 0:
        print("  \033[92m✓ OPT101s working. Add ADXL335 and run Test 5.\033[0m")
    else:
        print(f"  \033[91m✗ {n_fail} issue(s) — check VOUT pin wiring.\033[0m")
    print()

if __name__ == "__main__":
    main()
