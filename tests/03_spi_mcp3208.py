#!/usr/bin/env python3
"""
tests/03_spi_mcp3208.py
────────────────────────
Test 3 — MCP3208 SPI ADC with voltage divider.

Wiring:
  MCP3208 DIP-16 (notch at top-left):

  Left pins (1–8):          Right pins (9–16):
  1  CH0 ← voltage divider  16 VDD  → Pi 3.3V (pin 1)
  2  CH1                    15 VREF → Pi 3.3V (pin 1)
  3  CH2                    14 AGND → Pi GND  (pin 6)
  4  CH3                    13 CLK  → Pi GPIO11 SCLK (pin 23)
  5  CH4                    12 DOUT → Pi GPIO9  MISO (pin 21)
  6  CH5                    11 DIN  → Pi GPIO10 MOSI (pin 19)
  7  CH6                    10 /CS  → Pi GPIO8  CE0  (pin 24)
  8  CH7                     9 DGND → Pi GND  (pin 6)

  Voltage divider on CH0:
  Pi 3.3V → 10kΩ → CH0 pin 1 → 10kΩ → GND
  Expected: ~2048 counts (1.65V = half of 3.3V)

  Decoupling caps (from your kit):
  47µF electrolytic across VDD(pin16) and AGND(pin14)
  10µF electrolytic across VREF(pin15) and AGND(pin14)

Usage: python3 tests/03_spi_mcp3208.py
"""
import sys, time, statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, MCP3208

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<40} {detail}")
    return ok

def main():
    cfg  = load_config()
    setup_logging(cfg)
    hcfg = cfg["hardware"]

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 3 — SPI Bus + MCP3208 ADC                ║")
    print("╚══════════════════════════════════════════════════╝")
    print("\n  Wire: Pi 3.3V──10kΩ──CH0──10kΩ──GND\n")

    results = []
    import os
    results.append(chk("SPI device present", os.path.exists("/dev/spidev0.0"),
                        "/dev/spidev0.0"))

    adc = MCP3208(bus=hcfg["spi_bus"],
                  ce=hcfg["spi_ce_mcp3208_1"],
                  hz=hcfg["spi_hz"])

    # ── Voltage divider check ─────────────────────────────────
    print("  VOLTAGE DIVIDER ON CH0 (expect ~2048 counts = 1.65V)")
    vals = [adc.read(0) for _ in range(128)]
    m    = statistics.mean(vals)
    s    = statistics.stdev(vals)
    v    = m * 3.3 / 4096
    print(f"  Mean: {m:.1f} counts   Std: {s:.2f}   Voltage: {v:.3f}V\n")

    results.append(chk("CH0 reads ~midscale", 1700 < m < 2400,
                        f"{m:.0f} counts ({v:.3f}V) — expected 2048 (1.65V)"))
    results.append(chk("CH0 noise low",       s < 8,
                        f"std={s:.2f}  {'OK' if s < 8 else 'noisy — check connections'}"))

    # ── All 8 channels scan ───────────────────────────────────
    print("  ALL 8 CHANNELS SCAN")
    print(f"  {'Ch':<5} {'Mean':>7} {'Std':>7}  Status")
    stuck = False
    for ch in range(8):
        vals = [adc.read(ch) for _ in range(32)]
        m = statistics.mean(vals)
        s = statistics.stdev(vals)
        if m < 5:
            diag = "\033[91mSTUCK LOW — check GND\033[0m"
            stuck = True
        elif m > 4090:
            diag = "\033[91mSTUCK HIGH — check VCC\033[0m"
            stuck = True
        else:
            diag = "\033[92mOK\033[0m"
        print(f"  CH{ch}    {m:7.1f}  {s:7.2f}  {diag}")

    results.append(chk("No stuck channels", not stuck,
                        "all OK" if not stuck else "fix stuck channels before continuing"))

    # ── Throughput ────────────────────────────────────────────
    print("\n  THROUGHPUT (Pi Zero W target: ≥400 reads/sec total)")
    n, t0 = 0, time.monotonic()
    while time.monotonic() - t0 < 5.0:
        for ch in range(8):
            adc.read(ch)
        n += 8
    sps = n / 5.0
    per_ch = sps / 8
    results.append(chk("Throughput ≥ 400 total sps", sps >= 400,
                        f"{sps:.0f} reads/sec total ({per_ch:.0f}/ch)"))

    if sps < 400:
        print(f"  TIP: Lower adc_target_sps to {int(per_ch * 0.8)} in config.yaml")

    # ── Live display ──────────────────────────────────────────
    print("\n  LIVE READOUT — 5 seconds")
    print("  " + "  ".join(f"CH{c}" for c in range(8)))
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5.0:
            vals = [adc.read(ch) for ch in range(8)]
            print("\r  " + "  ".join(f"{v:4d}" for v in vals), end="", flush=True)
            time.sleep(0.1)
        print()
    except KeyboardInterrupt:
        print()

    adc.close()

    n_pass = sum(results)
    n_fail = len(results) - n_pass
    print(f"\n  {'─'*52}")
    print(f"  {n_pass}/{len(results)} passed")
    if n_fail == 0:
        print("  \033[92m✓ MCP3208 OK. Add OPT101s and run Test 4.\033[0m")
    else:
        print(f"  \033[91m✗ {n_fail} issue(s) — check SPI wiring.\033[0m")
    print()

if __name__ == "__main__":
    main()
