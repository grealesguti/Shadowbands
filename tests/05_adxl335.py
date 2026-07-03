#!/usr/bin/env python3
"""
tests/05_adxl335.py
────────────────────
Test 5 — BK-ADXL335 analog accelerometer via MCP3208.

Wiring (add to existing MCP3208 setup):
  BK-ADXL335 breakout:
    VCC  → Pi 3.3V (pin 1)
    GND  → Pi GND  (pin 6)
    XOUT → MCP3208 CH5 (pin 6)
    YOUT → MCP3208 CH6 (pin 7)
    ZOUT → MCP3208 CH7 (pin 8)

  Decoupling (from your cap kit):
    10µF electrolytic across VCC and GND on ADXL335

  Expected when flat on table:
    X ≈ 0g → ~2048 counts (~1.65V)
    Y ≈ 0g → ~2048 counts (~1.65V)
    Z ≈ +1g → ~2457 counts (~1.98V)

Usage: python3 tests/05_adxl335.py
       python3 tests/05_adxl335.py --simulate
"""
import sys, time, math, statistics, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, MCP3208, ADXL335

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<42} {detail}")
    return ok

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()

    cfg  = load_config()
    setup_logging(cfg)
    hcfg = cfg["hardware"]

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 5 — BK-ADXL335 Accelerometer            ║")
    print("╚══════════════════════════════════════════════════╝")
    print("\n  XOUT→CH5  YOUT→CH6  ZOUT→CH7\n")

    adc   = MCP3208(bus=hcfg["spi_bus"], ce=hcfg["spi_ce_mcp3208_1"],
                    hz=hcfg["spi_hz"], simulate=args.simulate)
    accel = ADXL335(adc,
                    ch_x=hcfg["adxl335_ch_x"],
                    ch_y=hcfg["adxl335_ch_y"],
                    ch_z=hcfg["adxl335_ch_z"],
                    vcc=hcfg["adxl335_vcc"])
    results = []

    # ── Test 1: Raw counts ────────────────────────────────────
    print("  TEST 1 — RAW ADC COUNTS")
    input("  Place ADXL335 flat on table → Enter ")
    time.sleep(0.5)

    raw = [accel.read_raw() for _ in range(100)]
    for axis, ch in [("x",5),("y",6),("z",7)]:
        vals = [r[axis] for r in raw]
        m    = statistics.mean(vals)
        s    = statistics.stdev(vals)
        ok   = 400 < m < 3700
        results.append(chk(f"CH{ch} ({axis.upper()}) not railed",
                            ok, f"{m:.0f} ± {s:.1f} counts"))

    # ── Test 2: Gravity vector ────────────────────────────────
    print("\n  TEST 2 — GRAVITY VECTOR (flat)")
    g_data = [accel.read() for _ in range(200)]
    gx = statistics.mean(r["x"] for r in g_data)
    gy = statistics.mean(r["y"] for r in g_data)
    gz = statistics.mean(r["z"] for r in g_data)
    g_mag = math.sqrt(gx**2 + gy**2 + gz**2)

    print(f"  ax={gx:+.3f}g  ay={gy:+.3f}g  az={gz:+.3f}g  |a|={g_mag:.3f}g")
    results.append(chk("Gravity magnitude ~1g",  0.8 < g_mag < 1.2,
                        f"{g_mag:.3f}g"))
    results.append(chk("ax ≈ 0 when flat",       abs(gx) < 0.35,
                        f"ax={gx:+.3f}g"))
    results.append(chk("ay ≈ 0 when flat",       abs(gy) < 0.35,
                        f"ay={gy:+.3f}g"))
    results.append(chk("az ≈ +1g (gravity)",     0.65 < gz < 1.35,
                        f"az={gz:+.3f}g"))

    # ── Test 3: Tilt ─────────────────────────────────────────
    print("\n  TEST 3 — TILT DETECTION")
    input("  Tilt ADXL335 so X axis points DOWN → Enter ")
    time.sleep(0.5)
    t = accel.read()
    print(f"  ax={t['x']:+.3f}g  ay={t['y']:+.3f}g  az={t['z']:+.3f}g")
    results.append(chk("Tilt detected on X", abs(t["x"]) > 0.5,
                        f"ax={t['x']:+.3f}g (should be ~±1g)"))

    # ── Test 4: Noise floor ───────────────────────────────────
    print("\n  TEST 4 — NOISE FLOOR (keep still)")
    input("  Place flat, do not touch table → Enter ")
    time.sleep(0.5)
    noise_data = [accel.read() for _ in range(500)]
    for axis in ["x", "y", "z"]:
        vals = [r[axis] for r in noise_data]
        s    = statistics.stdev(vals)
        ok   = s < 0.08
        results.append(chk(f"Noise {axis.upper()} < 80mg", ok,
                            f"std={s*1000:.1f}mg"))

    # ── Live display ──────────────────────────────────────────
    print("\n  LIVE DISPLAY — tilt it around for 8 seconds")
    input("  Press Enter to start... ")
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < 8.0:
            r    = accel.read()
            g    = math.sqrt(r["x"]**2 + r["y"]**2 + r["z"]**2)
            tilt = math.degrees(math.acos(max(-1, min(1, r["z"]/max(g,0.01)))))
            bar  = "█" * int(tilt / 3)
            print(f"\r  ax={r['x']:+.2f}g  ay={r['y']:+.2f}g  "
                  f"az={r['z']:+.2f}g  tilt={tilt:5.1f}°  {bar:<30}",
                  end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    print()

    adc.close()

    n_pass = sum(results)
    n_fail = len(results) - n_pass
    print(f"\n  {'─'*52}")
    print(f"  {n_pass}/{len(results)} passed")
    if n_fail == 0:
        print("  \033[92m✓ ADXL335 working. Run Test 6 (full pipeline).\033[0m")
    else:
        print(f"  \033[91m✗ {n_fail} issue(s) — check XOUT/YOUT/ZOUT wiring.\033[0m")
    print()

if __name__ == "__main__":
    main()
