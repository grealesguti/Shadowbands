#!/usr/bin/env python3
"""
tests/03b_opt101_debug.py
──────────────────────────
Test 3b — Single OPT101 on MCP3208, staged bring-up + circuit debug.
Works on Pi Zero W and Pi 3B+ (same SPI pins, same config.yaml).

Circuit under test (per your schematic):
  Pi 3.3V ── OPT101 Vs (pin 1)          + C2 bulk 1µF (+) to 3.3V, (−) to GND
  OPT101 pin 3 (−V) and pin 8 (Common) ── GND
  OPT101 pin 5 (Output) ── 10kΩ ──●── MCP3208 CH0 (pin 1)
                                  │
                             Cfilter 1µF (+) to signal node, (−) to GND
  NOTE: Cfilter must sit on the ADC side of the 10kΩ (at the ● node feeding
  CH0). Stage 5 of this test detects it if it is on the wrong side.

Expected numbers @ Vs = 3.3 V, VREF = 3.3 V, 12-bit (4096 counts):
  Fully dark         ≈  7–15 mV  →  ~9–19 counts   (0 counts = wiring fault!)
  Room light         ≈  hundreds of mV to ~2 V     (very setup-dependent)
  Saturated (bright) ≈  2.1–2.3 V →  ~2600–2850 counts
  It can NEVER reach 4095 at 3.3 V supply — output saturates ~1.1 V below Vs.
  A reading pinned at 4095 means CH0 is shorted to 3.3 V.

Usage:
  python3 tests/03b_opt101_debug.py            # OPT101 on CH0
  python3 tests/03b_opt101_debug.py --ch 2     # OPT101 on CH2
  python3 tests/03b_opt101_debug.py --skip-interactive
"""
import sys, time, statistics, argparse, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, MCP3208

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

VREF = 3.3
def volts(counts): return counts * VREF / 4096.0

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<42} {detail}")
    return ok

def warn(label, detail=""):
    print(f"  \033[93m! [WARN]\033[0m  {label:<42} {detail}")

def bar(counts, width=40, full=4096):
    n = int(counts / full * width)
    return "█" * n + "·" * (width - n)

def read_block(adc, ch, n, delay=0.0):
    vals = []
    for _ in range(n):
        vals.append(adc.read(ch))
        if delay: time.sleep(delay)
    return vals

def diagnose_level(m):
    """Map a mean reading to the most likely physical situation."""
    if m < 3:
        return ("\033[91mDEAD LOW\033[0m — 0 counts is not a valid dark level. "
                "Open wire to CH0, OPT101 unpowered, or output not connected.")
    if m < 25:
        return "dark-level output (normal if the sensor is covered)"
    if m > 4085:
        return ("\033[91mPINNED HIGH\033[0m — impossible with OPT101 at 3.3 V "
                "(saturates ~2850). CH0 shorted to 3.3 V or divider still wired?")
    if m > 2500:
        return "at/near optical saturation (~2.1–2.3 V) — normal in bright light"
    return "in the active range — good"

# ────────────────────────────────────────────────────────────────
def stage1_precheck(adc, ch):
    print("\n  STAGE 1 — SLOW SINGLE READS (1 per second, watch stability)")
    print(f"  {'#':<4}{'counts':>8}{'volts':>9}   level")
    vals = []
    for i in range(8):
        v = adc.read(ch)
        vals.append(v)
        print(f"  {i:<4}{v:>8}{volts(v):>8.3f}V   |{bar(v)}|")
        time.sleep(1.0)
    m = statistics.mean(vals)
    print(f"\n  Mean {m:.0f} counts ({volts(m):.3f} V) → {diagnose_level(m)}")
    ok_range = 3 <= m <= 4085
    chk("Reading in physically plausible range", ok_range, f"{m:.0f} counts")
    return ok_range, m

def stage2_dark_light(adc, ch, interactive=True):
    print("\n  STAGE 2 — DARK / LIGHT RESPONSE")
    if not interactive:
        warn("Skipped (non-interactive mode)")
        return True, None, None
    input("  → COVER the OPT101 completely (finger/tape/cap), then press Enter…")
    dark = statistics.mean(read_block(adc, ch, 256))
    print(f"    Dark:  {dark:7.1f} counts ({volts(dark)*1000:6.1f} mV)")
    input("  → Now EXPOSE it to room light or a lamp, then press Enter…")
    light = statistics.mean(read_block(adc, ch, 256))
    print(f"    Light: {light:7.1f} counts ({volts(light):6.3f} V)")

    ratio_ok = light > dark + 50
    r1 = chk("Sensor responds to light", ratio_ok,
             f"Δ = {light-dark:.0f} counts")
    r2 = True
    if dark < 3:
        r2 = chk("Dark level plausible (7–15 mV expected)", False,
                 "0 counts — see DEAD LOW diagnosis above")
    elif dark > 100:
        warn("Dark level high", f"{volts(dark)*1000:.0f} mV — light leak, "
             "or offset issue; true dark should be <~20 mV")
    if light > 2400:
        warn("Light reading near saturation",
             "fine for a functional test, but bands need headroom: "
             "aim for 30–70 %% of saturation when aimed at eclipse sky")
    return (r1 and r2), dark, light

def stage3_noise(adc, ch):
    print("\n  STAGE 3 — NOISE FLOOR (hold lighting constant, no hands moving)")
    vals = read_block(adc, ch, 1024)
    m, s = statistics.mean(vals), statistics.stdev(vals)
    vmin, vmax = min(vals), max(vals)
    print(f"    n=1024  mean={m:.1f}  std={s:.2f}  min={vmin}  max={vmax}  "
          f"p-p={vmax-vmin}")
    # 0.1% band contrast at mid-scale ≈ 2 counts → want std well under that
    ok = s < 4
    chk("Noise std < 4 counts", ok,
        f"std={s:.2f} ({'good' if s < 2 else 'acceptable' if ok else 'too noisy'})")
    if not ok:
        print("    Debug: shorten wires, verify Cfilter present & polarity,")
        print("           47µF on VDD close to chip, AGND/DGND both to Pi GND,")
        print("           and power the Pi from a good supply (not PC USB hub).")
    return ok

def stage4_rates(adc, ch):
    print("\n  STAGE 4 — SAMPLE-RATE SWEEP (keep lighting constant!)")
    print("  Watching for mean droop vs rate → RC/charge-injection problems")
    targets = [50, 100, 250, 500, 1000, 2000, 0]   # 0 = flat out
    print(f"  {'target':>7} {'achieved':>9} {'mean':>8} {'std':>7}")
    rows = []
    for t in targets:
        period = (1.0 / t) if t else 0.0
        n, t0, vals = 0, time.monotonic(), []
        while time.monotonic() - t0 < 2.0:
            tick = time.monotonic()
            vals.append(adc.read(ch)); n += 1
            if period:
                rem = period - (time.monotonic() - tick)
                if rem > 0: time.sleep(rem)
        ach = n / 2.0
        m, s = statistics.mean(vals), statistics.stdev(vals)
        rows.append((t, ach, m, s))
        print(f"  {t if t else 'max':>7} {ach:>9.0f} {m:>8.1f} {s:>7.2f}")

    slow_mean = rows[0][2]
    fast_mean = rows[-1][2]
    droop = slow_mean - fast_mean
    droop_ok = abs(droop) < max(8, 0.01 * slow_mean)
    chk("No mean droop at high rate", droop_ok,
        f"Δ(mean 50sps → max) = {droop:+.1f} counts")
    if not droop_ok:
        print("    Debug: this is the signature of Cfilter missing or placed on")
        print("           the OPT101 side of the 10kΩ. The ADC's sampling cap")
        print("           steals charge through 10kΩ each conversion. Move the")
        print("           1µF so it sits directly at the CH0 pin.")
    max_rate = rows[-1][1]
    rate_ok = max_rate >= 400
    chk("Max rate ≥ 400 sps (1 ch)", rate_ok, f"{max_rate:.0f} sps")
    if not rate_ok:
        print("    Debug: if reads are also garbage, lower spi_hz in config.yaml")
        print("           to 1000000 (MCP3208 clock limit ~1.3 MHz at 3.3 V).")
    return droop_ok and rate_ok, max_rate

def stage5_wave(adc, ch, max_rate, interactive=True, seconds=6.0):
    print("\n  STAGE 5 — DYNAMIC RESPONSE (the 4.5 Hz question)")
    if not interactive:
        warn("Skipped (non-interactive mode)")
        return True
    rate = min(200.0, max_rate * 0.8)
    input(f"  → WAVE your hand/fingers over the sensor for {seconds:.0f} s "
          "(try a steady ~4–5 waves/sec), press Enter to start…")
    period, samples, times = 1.0 / rate, [], []
    t0 = time.monotonic()
    while True:
        now = time.monotonic() - t0
        if now >= seconds: break
        samples.append(adc.read(ch)); times.append(now)
        rem = period - ((time.monotonic() - t0) - now)
        if rem > 0: time.sleep(rem)
    m = statistics.mean(samples)
    p2p = max(samples) - min(samples)
    print(f"    n={len(samples)} @ ~{len(samples)/seconds:.0f} sps   "
          f"mean={m:.0f}   p-p swing={p2p} counts")
    ok = p2p > 100
    chk("Sees hand-wave modulation (p-p > 100)", ok, f"p-p={p2p}")
    if HAVE_NUMPY and len(samples) > 64:
        x = np.array(samples, dtype=float)
        x -= x.mean()
        fs = len(samples) / seconds
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        freqs = np.fft.rfftfreq(len(x), 1.0 / fs)
        band = (freqs > 0.5) & (freqs < 20)
        if band.any():
            fpk = freqs[band][np.argmax(spec[band])]
            print(f"    Dominant modulation: {fpk:.2f} Hz "
                  f"(target band 0.5–20 Hz — shadowbands ≈ 4.5 Hz)")
    if not ok:
        print("    Debug: if Stage 2 passed but this fails, the signal path is")
        print("           over-filtered — check Cfilter isn't 100µF+ by mistake")
        print("           (10kΩ×1µF → fc≈16 Hz OK; 10kΩ×100µF → fc≈0.16 Hz BAD).")
    return ok

def stage6_live(adc, ch):
    print("\n  STAGE 6 — LIVE READOUT (Ctrl+C to stop)")
    print(f"  {'counts':>7} {'volts':>8}  level")
    try:
        while True:
            vals = read_block(adc, ch, 16)          # small avg for a calm display
            m = statistics.mean(vals)
            print(f"\r  {m:7.0f} {volts(m):7.3f}V  |{bar(m)}|", end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n  Live view stopped.")

# ────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, default=0, help="MCP3208 channel (default 0)")
    ap.add_argument("--skip-interactive", action="store_true")
    args = ap.parse_args()
    interactive = not args.skip_interactive

    cfg  = load_config()
    setup_logging(cfg)
    hcfg = cfg["hardware"]

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 3b — OPT101 bring-up & circuit debug       ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"\n  OPT101 → 10kΩ → CH{args.ch}, Cfilter 1µF at the ADC pin")
    print("  Reminder: remove the voltage-divider from this channel first!\n")

    results = []
    import os
    results.append(chk("SPI device present", os.path.exists("/dev/spidev0.0"),
                       "/dev/spidev0.0"))
    if not results[-1]:
        print("\n  Enable SPI: sudo raspi-config nonint do_spi 0  (then reboot)\n")
        sys.exit(1)

    adc = MCP3208(bus=hcfg["spi_bus"],
                  ce=hcfg["spi_ce_mcp3208_1"],
                  hz=hcfg["spi_hz"])

    ok1, mean1 = stage1_precheck(adc, args.ch)
    results.append(ok1)
    if mean1 < 3 or mean1 > 4085:
        print("\n  \033[91mStopping here — fix the wiring fault above before "
              "the other stages mean anything.\033[0m")
        print("  Multimeter checklist (Pi powered, sensor in room light):")
        print("   1. OPT101 pin 1 to GND ........ expect 3.30 V")
        print("   2. OPT101 pin 5 to GND ........ expect 0.05–2.2 V, varies w/ light")
        print("   3. MCP3208 CH pin to GND ...... expect ≈ same as pin 5")
        print("   4. If pin 5 ≈ 0 V in light: pins 3 & 8 both grounded? "
              "Chip in backwards? (dot = pin 1)")
        adc.close(); sys.exit(1)

    ok2, dark, light = stage2_dark_light(adc, args.ch, interactive)
    results.append(ok2)
    results.append(stage3_noise(adc, args.ch))
    ok4, max_rate = stage4_rates(adc, args.ch)
    results.append(ok4)
    results.append(stage5_wave(adc, args.ch, max_rate, interactive))

    n_pass = sum(results)
    print(f"\n  {'─'*54}")
    print(f"  {n_pass}/{len(results)} stages passed")
    if n_pass == len(results):
        print("  \033[92m✓ OPT101 chain healthy — Stage 6 live view, then "
              "wire the rest of the array.\033[0m")
    else:
        print("  \033[91m✗ Fix flagged stages before adding more sensors.\033[0m")

    if interactive:
        stage6_live(adc, args.ch)
    adc.close()

if __name__ == "__main__":
    main()
