#!/usr/bin/env python3
"""
03c_bpw34_quad_debug.py  --  Stage-0 bring-up & circuit debug for FOUR MCP3208
                             channels: TWO BPW34 photodiodes, each split into two
                             transimpedance gains (discrete op-amp front-end).

Extended from 03c_bpw34_lm358_debug.py (single channel). Reads four MCP3208
single-ended channels over SPI0/CE0 and walks staged checks, printing
PASS / FAIL / WARN with hints for the discrete front-end.

DEFAULT wiring assumed (override with flags):
    Diode A ─┬─ amp @ Rf=1.0M  -> CH0   (A high gain)
             └─ amp @ Rf=470k  -> CH1   (A low  gain)
    Diode B ─┬─ amp @ Rf=1.0M  -> CH2   (B high gain)
             └─ amp @ Rf=220k  -> CH3   (B low  gain)

  Per amp:  BPW34 cathode -> IN- summing node (anode -> GND)
            Rf from OUT to IN-,  small Cf across Rf (see feedback-cap table)
            IN+ -> Vref (0 V straight-to-GND, or the 220k/10k ~0.14 V divider
                         WITH its 100 nF bypass — pass --vbias to match)
            unused op-amp halves parked (IN- <-> OUT, IN+ -> GND)
    MCP3208 VDD/VREF -> +3.3 V, AGND/DGND -> GND

Why the pairing matters: the two channels on the SAME diode see IDENTICAL light,
so they must back-calculate to the SAME photocurrent (I_pd = (Vout - Vbias)/Rf).
That cross-check catches a wrong Rf, a swapped channel, or a railing amp. The two
DIODES are independent sensors (that's the science: a shadow reaches them at
different times).

Run:
  sudo python3 tests/03c_bpw34_quad_debug.py
  sudo python3 tests/03c_bpw34_quad_debug.py --rf 1e6 470e3 1e6 220e3 --chans 0 1 2 3
  sudo python3 tests/03c_bpw34_quad_debug.py --pairs 0,1 2,3 --vbias 0.14
  sudo python3 tests/03c_bpw34_quad_debug.py --skip-interactive
"""

import sys
import os
import time
import argparse

try:
    import numpy as np
except ImportError:
    print("This script needs numpy:  sudo pip3 install numpy --break-system-packages")
    sys.exit(1)

try:
    import spidev
except ImportError:
    print("This script needs spidev:  sudo pip3 install spidev --break-system-packages")
    sys.exit(1)


# --------------------------------------------------------------------------- #
#  MCP3208 reader  (unchanged from the single-channel script)
# --------------------------------------------------------------------------- #
class MCP3208:
    """Minimal single-ended reader for the MCP3208 12-bit ADC on SPI0."""

    def __init__(self, bus=0, dev=0, hz=1_000_000, vref=3.3):
        self.vref = vref
        self.spi = spidev.SpiDev()
        self.spi.open(bus, dev)
        self.spi.max_speed_hz = hz
        self.spi.mode = 0
        print(f"{time.strftime('%H:%M:%S')}  INFO     "
              f"MCP3208 bus{bus} CE{dev} @ {hz} Hz")

    def read(self, ch):
        cmd0 = 0b00000110 | ((ch & 0b100) >> 2)
        cmd1 = (ch & 0b011) << 6
        r = self.spi.xfer2([cmd0, cmd1, 0x00])
        return ((r[1] & 0x0F) << 8) | r[2]

    def volts(self, counts):
        return counts * self.vref / 4095.0

    def close(self):
        try:
            self.spi.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  pretty printers
# --------------------------------------------------------------------------- #
def hdr(chans, rfs, labels):
    print()
    print("\u2554" + "\u2550" * 56 + "\u2557")
    print("\u2551  03c \u2014 BPW34 QUAD (2 diodes \u00d7 2 gains) bring-up & debug  \u2551")
    print("\u255a" + "\u2550" * 56 + "\u255d")
    print()
    for lab, c, rf in zip(labels, chans, rfs):
        print(f"    {lab:>10s}  ->  CH{c}   (Rf = {_eng(rf)})")
    print()


def ok(desc, detail=""):
    print(f"  \u2713 [PASS]  {desc:<44s} {detail}")

def bad(desc, detail=""):
    print(f"  \u2717 [FAIL]  {desc:<44s} {detail}")

def warn(desc, detail=""):
    print(f"  ! [WARN]  {desc:<44s} {detail}")

def bar(counts, width=22):
    frac = max(0.0, min(1.0, counts / 4095.0))
    n = int(round(frac * width))
    return "|" + "\u2588" * n + "\u00b7" * (width - n) + "|"

def _eng(r):
    if r >= 1e6:
        return f"{r/1e6:g} M\u03a9"
    if r >= 1e3:
        return f"{r/1e3:g} k\u03a9"
    return f"{r:g} \u03a9"


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def read_row(adc, chans):
    """One interleaved scan across all channels -> list of counts."""
    return [adc.read(c) for c in chans]

def burst_all(adc, chans, n):
    """Read n interleaved rows; return (ndarray [n, nch], achieved_sps_total)."""
    buf = np.empty((n, len(chans)), dtype=np.int32)
    t0 = time.perf_counter()
    for i in range(n):
        for j, c in enumerate(chans):
            buf[i, j] = adc.read(c)
    dt = time.perf_counter() - t0
    return buf, (n / dt if dt > 0 else float("nan"))

def timed_all(adc, chans, target_sps, seconds):
    """Sample all channels at ~target_sps (per row) for `seconds`."""
    period = 1.0 / target_sps if target_sps > 0 else 0.0
    n = max(1, int(target_sps * seconds))
    buf = np.empty((n, len(chans)), dtype=np.int32)
    t0 = time.perf_counter()
    nxt = t0
    for i in range(n):
        for j, c in enumerate(chans):
            buf[i, j] = adc.read(c)
        nxt += period
        while time.perf_counter() < nxt:
            pass
    dt = time.perf_counter() - t0
    return buf, (n / dt if dt > 0 else float("nan"))

def photocurrent_nA(counts, rf, vbias, vref=3.3):
    """I_pd via one channel, in nA. Vout = Vbias + I_pd*Rf -> I_pd=(Vout-Vbias)/Rf."""
    vout = counts * vref / 4095.0
    return (vout - vbias) / rf * 1e9

def dominant_freq(x, fs, lo=0.5, hi=20.0):
    x = np.asarray(x, dtype=float)
    x = x - x.mean()
    if len(x) < 8:
        return None, 0.0
    win = np.hanning(len(x))
    X = np.abs(np.fft.rfft(x * win))
    f = np.fft.rfftfreq(len(x), d=1.0 / fs)
    band = (f >= lo) & (f <= hi)
    if not band.any():
        return None, 0.0
    k = int(np.argmax(X[band]))
    return float(f[band][k]), float(X[band][k])


# --------------------------------------------------------------------------- #
#  stages
# --------------------------------------------------------------------------- #
def stage1_precheck(adc, chans, rfs, labels, vbias):
    print("\n  STAGE 1 \u2014 SLOW SINGLE READS (1/s, watch stability)")
    print("  " + " ".join(f"{l:>11s}" for l in labels))
    for i in range(6):
        row = read_row(adc, chans)
        print(f"  {i:<2d}" + " ".join(f"{v:>6d}      " for v in row))
        time.sleep(0.8)
    buf, _ = burst_all(adc, chans, 128)
    means = buf.mean(axis=0)
    all_ok = True
    for lab, c, rf, m in zip(labels, chans, rfs, means):
        good = 3 <= m <= 4090
        all_ok &= good
        ipd = photocurrent_nA(m, rf, vbias)
        note = "" if good else "  \u2190 pinned at a rail"
        print(f"    {lab:>10s} CH{c}: {m:6.0f} ({adc.volts(m):.3f} V, "
              f"I_pd\u2248{ipd:8.0f} nA){note}")
    ok("All 4 readings in ADC range", all_ok) if all_ok else \
        bad("All 4 readings in ADC range", "fix wiring/oscillation on flagged CH")
    return all_ok, means

def stage2_dark_light(adc, chans, rfs, labels, vbias, interactive=True):
    print("\n  STAGE 2 \u2014 DARK / LIGHT RESPONSE (polarity + gain, per channel)")
    if not interactive:
        warn("Skipped (non-interactive mode)")
        return True, None, None
    input("  \u2192 COVER BOTH BPW34, then press Enter\u2026")
    dark = burst_all(adc, chans, 300)[0].mean(axis=0)
    input("  \u2192 EXPOSE both to light, then press Enter\u2026")
    light = burst_all(adc, chans, 300)[0].mean(axis=0)
    all_ok = True
    for j, (lab, c, rf) in enumerate(zip(labels, chans, rfs)):
        d = light[j] - dark[j]
        good = d > 50
        all_ok &= good
        note = "" if good else ("  \u2190 WRONG WAY (diode reversed)" if d < -50
                                 else "  \u2190 no response")
        print(f"    {lab:>10s} CH{c}: dark {dark[j]:6.1f} \u2192 light {light[j]:6.1f}"
              f"   \u0394={d:+7.0f}{note}")
        if dark[j] > 0.8 * 4095:
            warn(f"{lab} dark pinned HIGH", "reversed diode or amp latched")
        elif dark[j] - (vbias/3.3*4095) > 400:
            warn(f"{lab} dark high", f"{adc.volts(dark[j])*1000:.0f} mV \u2014 light leak/offset")
        if light[j] > 0.9 * 4095:
            print(f"      ({lab} SATURATED \u2014 expected for the high-gain 1M leg in "
                  "room light; comes on-scale only in dim/near-totality.)")
    ok("All 4 respond to light", all_ok) if all_ok else \
        bad("All 4 respond to light", "see per-channel notes")
    if not all_ok:
        print("    \u2192 For a dead channel: check Rf in place, BPW34 not reversed/open,")
        print("      IN+ at the intended Vref, and the summing node reaching IN-.")
    return all_ok, dark, light

def stage3_pair_crosscheck(adc, chans, rfs, labels, pairs, vbias, interactive=True):
    """The dual-gain money check: two gains on the SAME diode must agree on I_pd."""
    print("\n  STAGE 3 \u2014 SAME-DIODE I_pd CROSS-CHECK (two gains must agree)")
    if not interactive:
        warn("Skipped (non-interactive mode)")
        return True
    input("  \u2192 Hold a STEADY, DIM light on both diodes (so no 1M leg saturates), "
          "press Enter\u2026")
    means = burst_all(adc, chans, 400)[0].mean(axis=0)
    all_ok = True
    for (ja, jb) in pairs:
        ia = photocurrent_nA(means[ja], rfs[ja], vbias)
        ib = photocurrent_nA(means[jb], rfs[jb], vbias)
        # ratio of the two independent estimates of the same current
        hi, lo = max(abs(ia), abs(ib)), min(abs(ia), abs(ib))
        ratio = (lo / hi) if hi > 1e-9 else 0.0
        sat_a = means[ja] > 0.95 * 4095
        sat_b = means[jb] > 0.95 * 4095
        good = ratio > 0.75 and not (sat_a or sat_b)
        all_ok &= good
        tag = "agree" if good else ("one leg SATURATED" if (sat_a or sat_b)
                                    else "\u2717 DISAGREE")
        print(f"    {labels[ja]} (CH{chans[ja]}) I_pd={ia:8.0f} nA  vs  "
              f"{labels[jb]} (CH{chans[jb]}) I_pd={ib:8.0f} nA   ratio={ratio:.2f}  {tag}")
        if not good and not (sat_a or sat_b):
            print(f"      \u2192 The two gains on this diode disagree on the current. "
                  f"Check the Rf values ({_eng(rfs[ja])} / {_eng(rfs[jb])}) are actually")
            print("        fitted on the right channels, and neither amp is railing/leaking.")
    ok("Paired gains agree on I_pd", all_ok) if all_ok else \
        bad("Paired gains agree on I_pd", "dim the light, or fix Rf mapping")
    return all_ok

def stage4_independence(adc, chans, labels, pairs, light, interactive=True):
    """Cover ONE diode; only its two channels should drop -> mapping is right."""
    print("\n  STAGE 4 \u2014 DIODE INDEPENDENCE (cover one diode at a time)")
    if not interactive or light is None:
        warn("Skipped (needs interactive + Stage 2 light)")
        return True
    all_ok = True
    for pi, (ja, jb) in enumerate(pairs):
        dl = f"diode {chr(ord('A')+pi)}"
        input(f"  \u2192 EXPOSE both, then COVER only {dl} "
              f"(CH{chans[ja]}+CH{chans[jb]}), press Enter\u2026")
        cm = burst_all(adc, chans, 200)[0].mean(axis=0)
        drop = [light[j] - cm[j] for j in range(len(chans))]
        own = max(abs(drop[ja]), abs(drop[jb]))
        others = max((abs(drop[j]) for j in range(len(chans)) if j not in (ja, jb)),
                     default=0.0)
        good = own > 3 * others + 30
        all_ok &= good
        verdict = "ok" if good else "\u2717 CROSS-WIRED"
        print(f"    cover {dl}: own\u0394(max)={own:+6.0f}  other\u0394(max)={others:+6.0f}  {verdict}")
    ok("Diodes map to the right channels", all_ok) if all_ok else \
        bad("Diodes map to the right channels", "check sensor\u2194channel mapping")
    return all_ok

def stage5_noise(adc, chans, labels):
    print("\n  STAGE 5 \u2014 NOISE / OSCILLATION (hold light constant)")
    buf, sps = burst_all(adc, chans, 800)
    all_ok = True
    for j, (lab, c) in enumerate(zip(labels, chans)):
        v = buf[:, j]
        s = float(v.std()); pp = int(v.max() - v.min())
        good = s < 4
        all_ok &= good
        tag = "good" if s < 2 else ("acceptable" if good else "too noisy")
        print(f"    {lab:>10s} CH{c}: mean={v.mean():6.1f} std={s:5.2f} p-p={pp:4d} ({tag})")
        if not good and pp > 400:
            print(f"      \u2192 {lab}: large p-p looks like TIA OSCILLATION \u2014 add/adjust "
                  "Cf across that Rf.")
    ok("All 4 noise floors < 4 counts", all_ok) if all_ok else \
        bad("All 4 noise floors < 4 counts", "see flagged channels")
    return all_ok

def stage6_dynamic(adc, chans, labels, pairs, interactive=True, seconds=6.0):
    print("\n  STAGE 6 \u2014 DYNAMIC / SHADOW-BAND (wave a hand; two diodes = lag)")
    if not interactive:
        warn("Skipped (non-interactive mode)")
        return True
    input(f"  \u2192 Sweep a shadow/hand across BOTH diodes a few times over "
          f"{seconds:.0f} s, press Enter\u2026")
    buf, fs = timed_all(adc, chans, 200, seconds)
    all_ok = True
    for j, (lab, c) in enumerate(zip(labels, chans)):
        v = buf[:, j]
        pp = int(v.max() - v.min())
        fpk, _ = dominant_freq(v, fs)
        seen = pp > 40
        all_ok &= seen
        fs_txt = f" f\u2248{fpk:.2f} Hz" if fpk else ""
        print(f"    {lab:>10s} CH{c}: p-p={pp:4d}{fs_txt}   {'ok' if seen else 'too small'}")
    # optional inter-diode lag from the two HIGH-gain legs (first of each pair)
    if len(pairs) == 2 and fs and fs > 2:
        a = buf[:, pairs[0][0]].astype(float); b = buf[:, pairs[1][0]].astype(float)
        a -= a.mean(); b -= b.mean()
        if a.std() > 1e-6 and b.std() > 1e-6:
            a /= a.std(); b /= b.std()
            full = np.correlate(a, b, mode="full") / len(a)
            lags = np.arange(-len(a) + 1, len(a))
            m = int(fs * 0.5)
            sel = np.abs(lags) <= m
            k = int(np.argmax(np.abs(full[sel])))
            r = float(full[sel][k]); lag_ms = -lags[sel][k] / fs * 1000.0
            print(f"    diode A\u2013B  peak r={r:+.2f}  lag={lag_ms:+.0f} ms "
                  f"(a travelling shadow gives a nonzero lag)")
    ok("Both diodes see modulation", all_ok) if all_ok else \
        bad("Both diodes see modulation", "dim if saturated, or sweep harder")
    return all_ok

def stage7_live(adc, chans, rfs, labels, vbias):
    print("\n  STAGE 7 \u2014 LIVE 4-CH READOUT (Ctrl+C to stop)")
    try:
        while True:
            row = [np.mean([adc.read(c) for _ in range(6)]) for c in chans]
            line = "   ".join(
                f"{lab.split()[0]}{lab.split()[-1][0]}:{m:5.0f}|{bar(m,8)[1:-1]}|"
                for lab, m in zip(labels, row))
            print("\r  " + line, end="", flush=True)
            time.sleep(0.15)
    except KeyboardInterrupt:
        print("\n  Live view stopped.")


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def _parse_pairs(spec, nch):
    """--pairs '0,1' '2,3' -> [(0,1),(2,3)] as INDICES into the chans list."""
    pairs = []
    for s in spec:
        a, b = (int(x) for x in s.split(","))
        pairs.append((a, b))
    return pairs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chans", type=int, nargs=4, default=[0, 1, 2, 3],
                    help="four MCP3208 channels (default 0 1 2 3)")
    ap.add_argument("--rf", type=float, nargs=4, default=[1e6, 470e3, 1e6, 220e3],
                    help="Rf per channel, ohms (default 1e6 470e3 1e6 220e3)")
    ap.add_argument("--pairs", type=str, nargs=2, default=["0,1", "2,3"],
                    help="which channel INDICES share a diode (default '0,1' '2,3')")
    ap.add_argument("--vbias", type=float, default=0.0,
                    help="IN+ bias volts: 0 for straight-to-GND, 0.14 for the 220k/10k divider")
    ap.add_argument("--vref", type=float, default=3.3, help="ADC reference volts")
    ap.add_argument("--hz", type=int, default=1_000_000, help="SPI clock Hz")
    ap.add_argument("--skip-interactive", action="store_true")
    args = ap.parse_args()

    chans, rfs = args.chans, args.rf
    interactive = not args.skip_interactive
    pairs = _parse_pairs(args.pairs, len(chans))

    # labels like "A high", "A low", "B high", "B low" from the pairing + Rf
    labels = ["?"] * len(chans)
    for pi, (ja, jb) in enumerate(pairs):
        dn = chr(ord('A') + pi)
        hi, lo = (ja, jb) if rfs[ja] >= rfs[jb] else (jb, ja)
        labels[hi] = f"{dn} high"
        labels[lo] = f"{dn} low"

    hdr(chans, rfs, labels)
    if args.vbias == 0.0:
        print("  Vbias = 0 V (IN+ straight to GND). Dark \u2248 op-amp output floor.")
    else:
        print(f"  Vbias = {args.vbias:.3f} V (220k/10k divider) \u2014 needs its 100 nF "
              "bypass, else the reference floats and the amp won't respond.")
    print()

    dev_path = "/dev/spidev0.0"
    if os.path.exists(dev_path):
        ok("SPI device present", dev_path)
    else:
        bad("SPI device present", f"{dev_path} missing \u2014 enable SPI (raspi-config)")
        return

    adc = MCP3208(hz=args.hz, vref=args.vref)
    results = []

    ok1, means1 = stage1_precheck(adc, chans, rfs, labels, args.vbias)
    results.append(ok1)
    if any((m < 3 or m > 4090) for m in means1):
        print("\n  \033[91mStopping \u2014 a channel is at a rail; fix it before the rest "
              "means anything.\033[0m")
        print("  Per suspect CH (meter, powered, room light):")
        print("   1. op-amp V+ pin \u2192 GND ....... 3.30 V")
        print("   2. that amp IN+ \u2192 GND ........ %.2f V (your Vbias)" % args.vbias)
        print("   3. that amp OUT \u2192 GND ........ 0\u20133.3 V, varies with light")
        print("   4. that MCP3208 CH \u2192 GND ..... \u2248 same as OUT")
        adc.close()
        sys.exit(1)

    ok2, dark, light = stage2_dark_light(adc, chans, rfs, labels, args.vbias, interactive)
    results.append(ok2)
    results.append(stage3_pair_crosscheck(adc, chans, rfs, labels, pairs, args.vbias, interactive))
    results.append(stage4_independence(adc, chans, labels, pairs, light, interactive))
    results.append(stage5_noise(adc, chans, labels))
    results.append(stage6_dynamic(adc, chans, labels, pairs, interactive))

    n_pass = sum(results)
    print(f"\n  {'\u2500'*56}")
    print(f"  {n_pass}/{len(results)} stages passed")
    if n_pass == len(results):
        print("  \033[92m\u2713 All four channels healthy \u2014 both diodes dual-gain and "
              "independent.\033[0m")
    else:
        print("  \033[91m\u2717 Fix flagged stages before capture.\033[0m")

    if interactive:
        stage7_live(adc, chans, rfs, labels, args.vbias)
    adc.close()


if __name__ == "__main__":
    main()
