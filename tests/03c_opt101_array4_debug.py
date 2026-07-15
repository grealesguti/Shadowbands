#!/usr/bin/env python3
"""
tests/03c_opt101_array4_debug.py
─────────────────────────────────
Test 3c — FOUR OPT101 on MCP3208 (CH1–CH4), staged bring-up + array debug.
Sibling of 03b (single channel); same utils.hw / config.yaml / style.
Works on Pi Zero W and Pi 3B+.

Circuit under test (per channel n = 1..4):
  Pi 3.3V ── OPT101 Vs (pin 1)          + bulk 1µF (+) to 3.3V, (−) to GND
  OPT101 pin 3 (−V) and pin 8 (Common) ── GND
  OPT101 pin 5 (Output) ── 10kΩ ──●── MCP3208 CHn
                                  │
                             Cfilter 1µF (+) to signal node, (−) to GND
  Sensor map:  A→CH1  B→CH2  C→CH3  D→CH4   (override with --chans)

Why four together: the science is the CORRELATION between separated sensors.
A shadow sweeping the array shows up on all channels with increasing lags;
uncorrelated electronics/noise does not. Space the sensors a few cm apart so
a shadow reaches them at different times (touching sensors → zero lag → useless).

Expected @ Vs = 3.3 V, VREF = 3.3 V, 12-bit:
  dark ≈ 9–19 counts (0 = wiring fault) ; saturated ≈ 2600–2850 (never 4095).

Usage:
  python3 tests/03c_opt101_array4_debug.py
  python3 tests/03c_opt101_array4_debug.py --chans 1 2 3 4
  python3 tests/03c_opt101_array4_debug.py --skip-interactive
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
LABELS = ["A", "B", "C", "D"]
def volts(counts): return counts * VREF / 4096.0

def chk(label, ok, detail=""):
    icon   = "\033[92m✓\033[0m" if ok else "\033[91m✗\033[0m"
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  {icon} [{status}]  {label:<44} {detail}")
    return ok

def warn(label, detail=""):
    print(f"  \033[93m! [WARN]\033[0m  {label:<44} {detail}")

def bar(counts, width=22, full=4096):
    n = int(counts / full * width)
    return "█" * n + "·" * (width - n)

def diagnose_level(m):
    if m < 3:
        return "\033[91mDEAD LOW\033[0m — open wire to CH, OPT101 unpowered, or output not connected"
    if m < 25:
        return "dark-level (normal if covered)"
    if m > 4085:
        return "\033[91mPINNED HIGH\033[0m — CH shorted to 3.3 V or divider still wired"
    if m > 2500:
        return "near optical saturation — normal in bright light"
    return "active range — good"

# ── multi-channel readers ───────────────────────────────────────
def scan_block(adc, chans, n, delay=0.0):
    """Return list-of-lists: rows[n][nch] of raw counts (interleaved scan)."""
    rows = []
    for _ in range(n):
        rows.append([adc.read(c) for c in chans])
        if delay: time.sleep(delay)
    return rows

def col(rows, j):
    return [r[j] for r in rows]

def means_of(rows):
    return [statistics.mean(col(rows, j)) for j in range(len(rows[0]))]

# ── correlation helpers (shared with 03d logic, edge-safe) ──────
def _moving_avg(x, w):
    pad = w // 2
    xp = np.pad(x, pad, mode="edge")
    k = np.ones(w) / w
    return np.convolve(xp, k, mode="valid")[:len(x)]

def _detrend(x, fs, win_s=0.5):
    x = np.asarray(x, dtype=float)
    w = max(3, int(fs * win_s))
    if w >= len(x):
        return x - x.mean(), 0
    return x - _moving_avg(x, w), w // 2

def _xcorr(a, b, fs, maxlag_s=0.5, trim=0):
    """Normalised cross-correlation → (peak_r, lag_ms). lag>0 ⇒ b lags a."""
    if trim > 0 and len(a) > 2 * trim:
        a = a[trim:-trim]; b = b[trim:-trim]
    a = a - a.mean(); b = b - b.mean()
    sa, sb = a.std(), b.std()
    if sa < 1e-9 or sb < 1e-9:
        return 0.0, 0.0
    a = a / sa; b = b / sb
    full = np.correlate(a, b, mode="full") / len(a)
    lags = np.arange(-len(a) + 1, len(a))
    m = int(fs * maxlag_s)
    sel = np.abs(lags) <= m
    fl, ll = full[sel], lags[sel]
    k = int(np.argmax(np.abs(fl)))
    return float(fl[k]), -ll[k] / fs * 1000.0

# ── stages ──────────────────────────────────────────────────────
def stage1_precheck(adc, chans):
    print("\n  STAGE 1 — SLOW SINGLE READS (all 4 channels, watch stability)")
    print(f"  {'#':<4}" + "".join(f"{l:>6}" for l in LABELS))
    for i in range(6):
        row = [adc.read(c) for c in chans]
        print(f"  {i:<4}" + "".join(f"{v:>6}" for v in row))
        time.sleep(0.8)
    rows = scan_block(adc, chans, 128)
    ms = means_of(rows)
    all_ok = True
    for l, c, m in zip(LABELS, chans, ms):
        good = 3 <= m <= 4085
        all_ok &= good
        print(f"    {l} (CH{c}): {m:6.0f} ({volts(m):.3f} V) → {diagnose_level(m)}")
    chk("All 4 readings in plausible range", all_ok)
    return all_ok, ms

def stage2_dark_light(adc, chans, interactive=True):
    print("\n  STAGE 2 — DARK / LIGHT RESPONSE (per channel)")
    if not interactive:
        warn("Skipped (non-interactive mode)"); return True, None, None
    input("  → COVER all four OPT101, then press Enter…")
    dark = means_of(scan_block(adc, chans, 256))
    input("  → EXPOSE all four to light, then press Enter…")
    light = means_of(scan_block(adc, chans, 256))
    all_ok = True
    for j, (l, c) in enumerate(zip(LABELS, chans)):
        d = light[j] - dark[j]
        good = d > 50
        all_ok &= good
        note = "" if good else "  ← no response"
        print(f"    {l} (CH{c}): dark {dark[j]:6.1f} → light {light[j]:6.1f}  "
              f"Δ={d:+6.0f}{note}")
        if dark[j] < 3:
            print(f"      \033[91m{l}: 0 dark counts — 4→5 strap? Vs=3.3V? pins 3&8 GND?\033[0m")
        elif dark[j] > 100:
            warn(f"{l} dark high", f"{volts(dark[j])*1000:.0f} mV — light leak/offset")
        if light[j] > 2400:
            warn(f"{l} near saturation", "fine for bring-up; want 30–70% for the sky")
    chk("All 4 respond to light", all_ok)
    return all_ok, dark, light

def stage3_independence(adc, chans, light, interactive=True):
    print("\n  STAGE 3 — INDEPENDENCE (cover ONE sensor at a time)")
    print("  Confirms sensor↔channel mapping isn't swapped or cross-wired.")
    if not interactive or light is None:
        warn("Skipped (needs interactive + Stage 2 light levels)"); return True
    all_ok = True
    for idx, l in enumerate(LABELS):
        input(f"  → EXPOSE all, then COVER only sensor {l}, press Enter…")
        cm = means_of(scan_block(adc, chans, 200))
        drop = [light[j] - cm[j] for j in range(len(chans))]
        own = drop[idx]
        others = max(abs(drop[j]) for j in range(len(chans)) if j != idx)
        good = abs(own) > 3 * others + 30
        all_ok &= good
        verdict = "ok" if good else "\033[91mCROSS-TALK\033[0m"
        print(f"    cover {l}: Δ{l}={own:+6.0f}   max Δ(others)={others:+6.0f}   {verdict}")
    chk("Channels independent", all_ok,
        "" if all_ok else "check sensor↔CH mapping")
    return all_ok

def stage4_noise(adc, chans):
    print("\n  STAGE 4 — NOISE FLOOR (hold lighting constant)")
    rows = scan_block(adc, chans, 1000)
    all_ok = True
    for j, (l, c) in enumerate(zip(LABELS, chans)):
        v = col(rows, j)
        m, s = statistics.mean(v), statistics.stdev(v)
        pp = max(v) - min(v)
        good = s < 4
        all_ok &= good
        tag = "good" if s < 2 else ("acceptable" if good else "too noisy")
        print(f"    {l} (CH{c}): mean={m:6.1f}  std={s:5.2f}  p-p={pp:4d}  ({tag})")
    chk("All noise floors < 4 counts", all_ok)
    if not all_ok:
        print("    Debug: shorten wires, verify each Cfilter present & polarity,")
        print("           bulk cap on VDD near the chip, AGND/DGND to Pi GND,")
        print("           and a clean Pi supply (not a PC USB hub).")
    return all_ok

def stage5_correlation(adc, chans, interactive=True, seconds=8.0):
    print("\n  STAGE 5 — CROSS-CORRELATION (sweep a shadow across A→B→C→D)")
    if not interactive:
        warn("Skipped (non-interactive mode)"); return True
    if not HAVE_NUMPY:
        warn("numpy missing — cannot correlate", "pip3 install numpy"); return True
    rate = 200.0
    input(f"  → Sweep a shadow/hand along the array a few times over {seconds:.0f} s, "
          "press Enter to start…")
    period, rows, t0 = 1.0 / rate, [], time.monotonic()
    while time.monotonic() - t0 < seconds:
        tick = time.monotonic()
        rows.append([adc.read(c) for c in chans])
        rem = period - (time.monotonic() - tick)
        if rem > 0: time.sleep(rem)
    fs = len(rows) / seconds
    cols = [np.array(col(rows, j), float) for j in range(len(chans))]
    pps = [int(c.max() - c.min()) for c in cols]
    det, trim = [], 0
    for c in cols:
        d, tr = _detrend(c, fs); det.append(d); trim = tr
    print(f"    n={len(rows)} @ ~{fs:.0f} sps   p-p: " +
          "  ".join(f"{l}={p}" for l, p in zip(LABELS, pps)))
    print("    pair    peak r    lag(vs A)")
    corr_ok, lags = True, [0.0]
    for j in range(1, len(chans)):
        r, lag = _xcorr(det[0], det[j], fs, trim=trim)
        lags.append(lag)
        good = abs(r) > 0.4
        corr_ok &= good
        print(f"     A–{LABELS[j]}    {r:+.2f}    {lag:+6.0f} ms   {'ok' if good else 'weak'}")
    mono = (all(lags[i] <= lags[i+1] for i in range(len(lags)-1)) or
            all(lags[i] >= lags[i+1] for i in range(len(lags)-1)))
    ok = corr_ok and min(pps) > 40
    chk("Shadow correlates across the array", ok)
    if ok and mono:
        print("    → Lags monotonic A→D: a clean travelling shadow — direction &")
        print("      speed are recoverable. This is exactly the detection principle.")
    elif ok and not mono:
        print("    → Correlated but lags not monotonic — check the physical sensor")
        print("      order matches A,B,C,D and sweep more steadily.")
    else:
        print("    → Weak/absent: sweep more distinctly, space sensors more, or dim")
        print("      the light if channels are saturated.")
    return ok

def stage6_matching(adc, chans, interactive=True):
    print("\n  STAGE 6 — CHANNEL MATCHING (even light → similar readings)")
    if not interactive:
        warn("Skipped (non-interactive mode)"); return True
    input("  → Illuminate all four AS EVENLY as possible, press Enter…")
    ms = means_of(scan_block(adc, chans, 256))
    for l, c, m in zip(LABELS, chans, ms):
        print(f"    {l} (CH{c}): {m:6.0f}  |{bar(m)}|")
    spread = (max(ms) - min(ms)) / max(1.0, statistics.mean(ms)) * 100
    good = spread < 25
    chk("Channels reasonably matched", good, f"{spread:.1f}% spread")
    if not good:
        print("    → Uneven lighting or OPT101 tolerance. Record per-channel gains")
        print("      so the analysis can normalise them.")
    return good

def stage7_live(adc, chans):
    print("\n  STAGE 7 — LIVE 4-CH READOUT (Ctrl+C to stop)")
    try:
        while True:
            row = [statistics.mean([adc.read(c) for _ in range(8)]) for c in chans]
            line = "   ".join(f"{l}:{m:5.0f}|{bar(m,10)}|" for l, m in zip(LABELS, row))
            print("\r  " + line, end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n  Live view stopped.")

# ────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chans", type=int, nargs=4, default=[1, 2, 3, 4],
                    help="four MCP3208 channels (default 1 2 3 4)")
    ap.add_argument("--skip-interactive", action="store_true")
    args = ap.parse_args()
    interactive = not args.skip_interactive
    chans = args.chans

    cfg  = load_config()
    setup_logging(cfg)
    hcfg = cfg["hardware"]

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 3c — 4× OPT101 array bring-up & correlation ║")
    print("╚══════════════════════════════════════════════════╝")
    print("\n  " + "   ".join(f"{l}→CH{c}" for l, c in zip(LABELS, chans)))
    print("  Reminder: remove any voltage-dividers; space sensors a few cm apart.\n")

    import os
    if not chk("SPI device present", os.path.exists("/dev/spidev0.0"), "/dev/spidev0.0"):
        print("\n  Enable SPI: sudo raspi-config nonint do_spi 0  (then reboot)\n")
        sys.exit(1)

    adc = MCP3208(bus=hcfg["spi_bus"],
                  ce=hcfg["spi_ce_mcp3208_1"],
                  hz=hcfg["spi_hz"])

    results = []
    ok1, means1 = stage1_precheck(adc, chans)
    results.append(ok1)
    if any((m < 3 or m > 4085) for m in means1):
        print("\n  \033[91mStopping — a channel has a wiring fault; fix it before the "
              "other stages mean anything.\033[0m")
        print("  Per suspect channel, multimeter (Pi powered, room light):")
        print("   1. OPT101 pin 1 → GND ....... 3.30 V")
        print("   2. OPT101 pin 5 → GND ....... 0.05–2.2 V, varies with light")
        print("   3. that MCP3208 CH pin → GND  ≈ same as pin 5")
        print("   4. pin 5 ≈ 0 V in light → pins 3 & 8 grounded? chip backwards? 4→5 strap?")
        adc.close(); sys.exit(1)

    ok2, dark, light = stage2_dark_light(adc, chans, interactive)
    results.append(ok2)
    results.append(stage3_independence(adc, chans, light, interactive))
    results.append(stage4_noise(adc, chans))
    results.append(stage5_correlation(adc, chans, interactive))
    results.append(stage6_matching(adc, chans, interactive))

    n_pass = sum(results)
    print(f"\n  {'─'*54}")
    print(f"  {n_pass}/{len(results)} stages passed")
    if n_pass == len(results):
        print("  \033[92m✓ 4-channel array healthy — mount on the L-frame and add "
              "orientation logging.\033[0m")
    else:
        print("  \033[91m✗ Fix flagged stages before deploying the array.\033[0m")

    if interactive:
        stage7_live(adc, chans)
    adc.close()

if __name__ == "__main__":
    main()
