#!/usr/bin/env python3
"""
04_bpw34_mcp6002_dual_calibrate.py -- Bring-up, debug & Rf CALIBRATION for the
                                       dual-gain BPW34 + MCP6002 front-end.

One BPW34 drives BOTH halves of an MCP6002 as two transimpedance amplifiers
with different feedback resistors, read simultaneously on two MCP3208 channels:

    CH0  <- amp A  (LOW gain,  Rf_A, bright / normal light)
    CH1  <- amp B  (HIGH gain, Rf_B, dim / near-totality, shadow-band contrast)

Both inverting inputs share the photodiode summing node; both non-inverting
inputs sit at a shared Vref (~0.14 V divider) so the single-supply output has
head-room above 0 V.

What this script does
---------------------
  * probes SPI + both channels,
  * checks polarity / gain / noise on EACH channel,
  * measures the ACTUAL photocurrent (I_pd) at the light levels you show it, and
  * RECOMMENDS the feedback resistors Rf_A (bright) and Rf_B (dim) so each
    channel lands at a chosen fraction of full-scale in its target condition.

The key idea: a TIA output is  Vout = Vref + I_pd * Rf.  If you know Rf (the one
currently fitted) and read Vout, you get I_pd = (Vout - Vref) / Rf, independent
of gain.  Once I_pd is known for "bright" and for "dim", the Rf that hits a
target voltage is simply  Rf = (Vtarget - Vref) / I_pd.

Run:
    sudo python3 04_bpw34_mcp6002_dual_calibrate.py \
         --ch-low 0 --ch-high 1 \
         --rf-low 100e3 --rf-high 2.2e6 --vref-bias 0.14
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
#  Preferred E12 / common resistor values, for snapping recommendations to
#  something you can actually buy.
# --------------------------------------------------------------------------- #
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
DECADES = [1e3, 1e4, 1e5, 1e6, 1e7]
STOCK_RF = sorted({round(v * d) for d in DECADES for v in E12})


def nearest_e12(r):
    """Nearest standard E12 resistor to r (ohms), within the stocked range."""
    r = max(STOCK_RF[0], min(STOCK_RF[-1], r))
    return min(STOCK_RF, key=lambda s: abs(s - r))


def eng(r):
    """Human resistor string, e.g. 2.2 M, 470 k, 100 k."""
    if r >= 1e6:
        return f"{r/1e6:.3g} M\u03a9"
    if r >= 1e3:
        return f"{r/1e3:.3g} k\u03a9"
    return f"{r:.0f} \u03a9"


# --------------------------------------------------------------------------- #
#  MCP3208 reader
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
        # single-ended, channel 0..7
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
def hdr():
    print()
    print("\u2554" + "\u2550" * 58 + "\u2557")
    print("\u2551  04 \u2014 BPW34 + MCP6002 DUAL-GAIN bring-up & Rf calibrate   \u2551")
    print("\u255a" + "\u2550" * 58 + "\u255d")
    print()
    print("  1 BPW34 \u2192 shared summing node \u2192 MCP6002 (both op-amps)")
    print("  CH0 = LOW gain  (Rf_A, bright)   CH1 = HIGH gain (Rf_B, dim)")
    print()


def ok(desc, detail=""):
    print(f"  \u2713 [PASS]  {desc:<44s} {detail}")


def bad(desc, detail=""):
    print(f"  \u2717 [FAIL]  {desc:<44s} {detail}")


def warn(desc, detail=""):
    print(f"  ! [WARN]  {desc:<44s} {detail}")


def bar(counts, width=32):
    frac = max(0.0, min(1.0, counts / 4095.0))
    n = int(round(frac * width))
    return "|" + "\u2588" * n + "\u00b7" * (width - n) + "|"


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def burst(adc, ch, n):
    """Read n samples as fast as possible; return (array, achieved_sps)."""
    buf = np.empty(n, dtype=np.int32)
    t0 = time.perf_counter()
    for i in range(n):
        buf[i] = adc.read(ch)
    dt = time.perf_counter() - t0
    return buf, (n / dt if dt > 0 else float("nan"))


def burst2(adc, ch_a, ch_b, n):
    """Interleaved read of two channels; returns (arr_a, arr_b, pair_sps)."""
    a = np.empty(n, dtype=np.int32)
    b = np.empty(n, dtype=np.int32)
    t0 = time.perf_counter()
    for i in range(n):
        a[i] = adc.read(ch_a)
        b[i] = adc.read(ch_b)
    dt = time.perf_counter() - t0
    return a, b, (n / dt if dt > 0 else float("nan"))


def timed2(adc, ch_a, ch_b, target_sps, seconds):
    """Interleaved sampling of two channels at ~target_sps per channel."""
    period = 1.0 / target_sps if target_sps > 0 else 0.0
    n = max(1, int(target_sps * seconds))
    a = np.empty(n, dtype=np.int32)
    b = np.empty(n, dtype=np.int32)
    t0 = time.perf_counter()
    nxt = t0
    for i in range(n):
        a[i] = adc.read(ch_a)
        b[i] = adc.read(ch_b)
        nxt += period
        while time.perf_counter() < nxt:
            pass
    dt = time.perf_counter() - t0
    return a, b, (n / dt if dt > 0 else float("nan"))


def dominant_freq(x, fs, lo=0.5, hi=20.0):
    """Peak spectral frequency of detrended x within [lo, hi] Hz."""
    x = x.astype(float)
    x = x - x.mean()
    if len(x) < 8:
        return None, 0.0
    win = np.hanning(len(x))
    X = np.abs(np.fft.rfft(x * win))
    f = np.fft.rfftfreq(len(x), d=1.0 / fs)
    band = (f >= lo) & (f <= hi)
    if not band.any():
        return None, 0.0
    k = np.argmax(X[band])
    return f[band][k], X[band][k]


def photocurrent(v_out, v_ref_bias, rf):
    """I_pd from a TIA output voltage:  I_pd = (Vout - Vbias) / Rf. Returns amps."""
    return (v_out - v_ref_bias) / rf


# --------------------------------------------------------------------------- #
#  per-channel checks (polarity, gain, noise) -- reused for CH0 and CH1
# --------------------------------------------------------------------------- #
def check_channel(adc, ch, name, rf, vbias):
    """Runs dark/light + noise checks on one channel. Returns a dict of results."""
    res = {"name": name, "ch": ch, "rf": rf,
           "dark": None, "light": None, "delta": None,
           "std": None, "pp": None, "sat_light": False, "pass": 0}

    print(f"\n  \u2500\u2500 {name}  (CH{ch}, Rf fitted = {eng(rf)}) "
          + "\u2500" * 8)

    # dark
    input(f"  \u2192 COVER the BPW34, then press Enter (testing {name})\u2026")
    dark, _ = burst(adc, ch, 400)
    dmean = float(dark.mean())
    res["dark"] = dmean
    print(f"    Dark:   {dmean:7.1f} counts ({adc.volts(dmean)*1000:6.1f} mV)  {bar(int(dmean))}")

    # light
    input("  \u2192 Now EXPOSE it to the intended light, then press Enter\u2026")
    light, _ = burst(adc, ch, 400)
    lmean = float(light.mean())
    res["light"] = lmean
    print(f"    Light:  {lmean:7.1f} counts ({adc.volts(lmean)*1000:6.1f} mV)  {bar(int(lmean))}")

    delta = lmean - dmean
    res["delta"] = delta
    if delta > 100:
        ok("Output rises with light", f"\u0394 = +{delta:.0f} counts")
        res["pass"] += 1
    elif delta < -50:
        bad("Output rises with light", f"\u0394 = {delta:.0f} counts \u2014 WRONG WAY")
        print("    \u2192 BPW34 reversed: CATHODE must go to the summing node")
        print("      (MCP6002 \u2013IN, pin 2 for amp A / pin 6 for amp B); ANODE \u2192 GND.")
    else:
        bad("Output rises with light", f"\u0394 = {delta:.0f} counts \u2014 no response")
        print(f"    \u2192 Check Rf in place on this amp, shared Vref bias present,")
        print(f"      and the summing node actually reaching this op-amp input.")

    # dark-floor sanity vs the shared bias
    if dmean > 0.9 * 4095:
        warn("Dark pinned HIGH", "reversed diode or amp latched at rail")
    elif adc.volts(dmean) > vbias + 0.15:
        warn("Dark above bias", f"{adc.volts(dmean)*1000:.0f} mV vs Vbias "
             f"{vbias*1000:.0f} mV \u2014 light leak or offset")

    if lmean > 0.95 * 4095:
        res["sat_light"] = True
        print(f"    (CH{ch} SATURATED at this light \u2014 expected for the HIGH-gain amp"
              if rf > 5e5 else
              f"    (CH{ch} SATURATED \u2014 too much gain for this light level)")
        print("     ; calibration below will tell you how far to drop Rf.)")

    # noise / oscillation
    quiet, sps_q = burst(adc, ch, 1024)
    qstd = float(quiet.std())
    qpp = int(quiet.max() - quiet.min())
    res["std"] = qstd
    res["pp"] = qpp
    print(f"    Noise:  std={qstd:.2f}  p-p={qpp}  (@~{sps_q:.0f} sps)")
    if qstd < 4:
        ok("Noise std < 4 counts", f"std={qstd:.2f}")
        res["pass"] += 1
    elif qpp > 800:
        bad("Noise std < 4 counts", f"std={qstd:.1f}, p-p={qpp} \u2014 OSCILLATION")
        print("    \u2192 High-Z TIA with BPW34 (~70 pF) needs a feedback cap Cf.")
        print(f"      For a ~200 Hz pole at Rf={eng(rf)}:  Cf \u2248 "
              f"{1.0/(2*np.pi*rf*200)*1e12:.0f} pF across this Rf.")
    else:
        warn("Noise std < 4 counts", f"std={qstd:.1f} \u2014 higher than ideal")

    return res


# --------------------------------------------------------------------------- #
#  calibration: turn a measured light level into an Rf recommendation
# --------------------------------------------------------------------------- #
def calibrate_gain(adc, ch, rf_fitted, vbias, target_frac, condition, vref):
    """
    Measure I_pd at the CURRENT light on `ch` (which has rf_fitted installed),
    then recommend the Rf that puts the output at target_frac of full-scale.

    Returns (i_pd, rf_recommended, rf_e12).
    """
    input(f"  \u2192 Set the '{condition}' light on the sensor, then press Enter\u2026")
    samp, _ = burst(adc, ch, 600)
    cmean = float(samp.mean())
    vout = adc.volts(cmean)
    i_pd = photocurrent(vout, vbias, rf_fitted)

    print(f"    {condition:<12s} CH{ch}: {cmean:7.1f} counts "
          f"({vout*1000:6.1f} mV)  \u2192  I_pd = {i_pd*1e9:8.2f} nA")

    if cmean > 0.98 * 4095:
        warn("Channel saturated during calibration",
             "I_pd is a LOWER BOUND \u2014 drop Rf and re-run for an accurate figure")
    if i_pd <= 0:
        bad("Non-positive photocurrent", "reversed diode or no light delta")
        return i_pd, None, None

    v_target = target_frac * vref            # e.g. 0.8 * 3.3 V
    rf_reco = (v_target - vbias) / i_pd
    rf_e12 = nearest_e12(rf_reco)
    v_e12 = vbias + i_pd * rf_e12
    print(f"      target {target_frac*100:.0f}% FS ({v_target:.2f} V) "
          f"\u2192 Rf = {eng(rf_reco)}  \u2192 nearest E12 {eng(rf_e12)} "
          f"(gives {v_e12:.2f} V, {v_e12/vref*100:.0f}% FS)")
    # suggested Cf for a ~200 Hz anti-alias-friendly pole
    cf = 1.0 / (2 * np.pi * rf_e12 * 200)
    print(f"      pair with Cf \u2248 {cf*1e12:.0f} pF across Rf for a ~200 Hz pole "
          f"(good for ~500 Hz sampling)")
    return i_pd, rf_reco, rf_e12


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch-low", type=int, default=0,
                    help="MCP3208 channel for the LOW-gain (bright) amp A (default 0)")
    ap.add_argument("--ch-high", type=int, default=1,
                    help="MCP3208 channel for the HIGH-gain (dim) amp B (default 1)")
    ap.add_argument("--rf-low", type=float, default=100e3,
                    help="Rf currently fitted on the LOW-gain amp, ohms (default 100e3)")
    ap.add_argument("--rf-high", type=float, default=2.2e6,
                    help="Rf currently fitted on the HIGH-gain amp, ohms (default 2.2e6)")
    ap.add_argument("--vref-bias", type=float, default=0.14,
                    help="Shared non-inverting bias voltage, V (default 0.14)")
    ap.add_argument("--target-low", type=float, default=0.80,
                    help="Target full-scale fraction for bright light on LOW gain (default 0.80)")
    ap.add_argument("--target-high", type=float, default=0.80,
                    help="Target full-scale fraction for dim light on HIGH gain (default 0.80)")
    ap.add_argument("--vref", type=float, default=3.3, help="ADC reference volts")
    ap.add_argument("--hz", type=int, default=1_000_000, help="SPI clock Hz")
    ap.add_argument("--sample-rate", type=int, default=500,
                    help="Per-channel sample rate target for dynamic tests (default 500)")
    args = ap.parse_args()

    cl, chh = args.ch_low, args.ch_high
    vbias = args.vref_bias

    hdr()

    # ---- SPI present -----------------------------------------------------
    dev_path = "/dev/spidev0.0"
    if os.path.exists(dev_path):
        ok("SPI device present", dev_path)
    else:
        bad("SPI device present", f"{dev_path} missing \u2014 enable SPI (raspi-config)")
        return

    adc = MCP3208(hz=args.hz, vref=args.vref)

    # ================================================================== #
    #  STAGE 1 -- both channels present & in range
    # ================================================================== #
    print("\n  STAGE 1 \u2014 BOTH CHANNELS PRESENT (slow reads)")
    print("   #     CH%d(low)          CH%d(high)" % (cl, chh))
    lows, highs = [], []
    for i in range(6):
        a = adc.read(cl); b = adc.read(chh)
        lows.append(a); highs.append(b)
        print(f"   {i:<3d}  {a:5d} {adc.volts(a):.3f}V {bar(a,18)}   "
              f"{b:5d} {adc.volts(b):.3f}V {bar(b,18)}")
        time.sleep(0.8)
    lmean0 = np.mean(lows); hmean0 = np.mean(highs)
    stage1_ok = True
    for nm, m, cch in (("LOW/CH%d" % cl, lmean0, cl), ("HIGH/CH%d" % chh, hmean0, chh)):
        if 5 <= m <= 4090:
            ok(f"{nm} in ADC range", f"{m:.0f} counts ({adc.volts(m):.3f} V)")
        else:
            bad(f"{nm} in ADC range", f"{m:.0f} counts \u2014 pinned; check wiring/osc")
            stage1_ok = False

    # ================================================================== #
    #  STAGE 2 -- per-channel polarity / gain / noise
    # ================================================================== #
    print("\n  STAGE 2 \u2014 PER-CHANNEL POLARITY, GAIN & NOISE")
    res_low = check_channel(adc, cl, "LOW gain (bright)", args.rf_low, vbias)
    res_high = check_channel(adc, chh, "HIGH gain (dim)", args.rf_high, vbias)

    # cross-check: both amps see the SAME photodiode, so I_pd must agree
    print("\n  \u2500\u2500 shared-photodiode cross-check " + "\u2500" * 20)
    if res_low["light"] and res_high["light"]:
        vlo = adc.volts(res_low["light"]); vhi = adc.volts(res_high["light"])
        ipd_lo = photocurrent(vlo, vbias, args.rf_low)
        ipd_hi = photocurrent(vhi, vbias, args.rf_high)
        print(f"    I_pd via LOW  ({eng(args.rf_low)}): {ipd_lo*1e9:8.2f} nA")
        print(f"    I_pd via HIGH ({eng(args.rf_high)}): {ipd_hi*1e9:8.2f} nA"
              + ("   [HIGH saturated \u2014 ignore]" if res_high["sat_light"] else ""))
        if not res_high["sat_light"] and ipd_lo > 0 and ipd_hi > 0:
            ratio = ipd_hi / ipd_lo
            if 0.5 < ratio < 2.0:
                ok("Both amps agree on I_pd", f"ratio {ratio:.2f}")
            else:
                warn("Amps disagree on I_pd", f"ratio {ratio:.2f} \u2014 "
                     "check Rf values, or one amp railing/leaking")

    # ================================================================== #
    #  STAGE 3 -- Rf CALIBRATION for each light condition
    # ================================================================== #
    print("\n  STAGE 3 \u2014 Rf CALIBRATION (measure I_pd, recommend resistors)")
    print("   The LOW-gain amp is for BRIGHT light; HIGH-gain for DIM light.")
    print("   Show each condition when prompted. Read the recommended Rf at the end.\n")

    print("  [A] BRIGHT / normal-light calibration on the LOW-gain channel:")
    ipd_bright, rf_lo_reco, rf_lo_e12 = calibrate_gain(
        adc, cl, args.rf_low, vbias, args.target_low, "bright", args.vref)

    print("\n  [B] DIM / near-totality calibration on the HIGH-gain channel:")
    print("      (dim the room right down, or shade to mimic deep partial eclipse)")
    ipd_dim, rf_hi_reco, rf_hi_e12 = calibrate_gain(
        adc, chh, args.rf_high, vbias, args.target_high, "dim", args.vref)

    # ================================================================== #
    #  STAGE 4 -- simultaneous dual-channel dynamic test (shadow-band sim)
    # ================================================================== #
    print("\n  STAGE 4 \u2014 SIMULTANEOUS DUAL-CHANNEL DYNAMIC (the 4.5 Hz question)")
    input("  \u2192 WAVE fingers over the sensor ~4\u20135/s for 6 s, press Enter\u2026")
    a, b, fs = timed2(adc, cl, chh, args.sample_rate, 6.0)
    ppa = int(a.max() - a.min()); ppb = int(b.max() - b.min())
    fpk_a, _ = dominant_freq(a, fs); fpk_b, _ = dominant_freq(b, fs)
    print(f"    per-channel ~{fs:.0f} sps  ({len(a)} samples each)")
    print(f"    LOW /CH{cl}:  mean={a.mean():.0f}  p-p={ppa}  "
          f"f\u2248{fpk_a:.2f} Hz" if fpk_a else f"    LOW /CH{cl}:  p-p={ppa}")
    print(f"    HIGH/CH{chh}: mean={b.mean():.0f}  p-p={ppb}  "
          f"f\u2248{fpk_b:.2f} Hz" if fpk_b else f"    HIGH/CH{chh}: p-p={ppb}")
    if max(ppa, ppb) > 100:
        ok("Modulation seen on \u2265 1 channel", f"p-p L={ppa} H={ppb}")
    else:
        bad("Modulation seen", f"p-p L={ppa} H={ppb} \u2014 saturated or too dim")
    if fs < 0.8 * args.sample_rate:
        warn("Sample rate below target",
             f"{fs:.0f}/{args.sample_rate} sps per ch \u2014 lower --hz load or SPI speed")

    # ================================================================== #
    #  SUMMARY -- the numbers you actually wanted
    # ================================================================== #
    print("\n  " + "\u2550" * 60)
    print("  CALIBRATION SUMMARY")
    print("  " + "\u2500" * 60)
    if ipd_bright and ipd_bright > 0:
        print(f"  BRIGHT  I_pd \u2248 {ipd_bright*1e9:8.2f} nA")
        if rf_lo_e12:
            v = vbias + ipd_bright * rf_lo_e12
            print(f"    LOW-gain  Rf \u2192 {eng(rf_lo_e12):>8s}  "
                  f"(target {args.target_low*100:.0f}% FS, gives {v/args.vref*100:.0f}% "
                  f"= {v:.2f} V)")
            print(f"              Cf \u2248 {1.0/(2*np.pi*rf_lo_e12*200)*1e12:.0f} pF "
                  f"(~200 Hz pole)")
    if ipd_dim and ipd_dim > 0:
        print(f"  DIM     I_pd \u2248 {ipd_dim*1e9:8.2f} nA")
        if rf_hi_e12:
            v = vbias + ipd_dim * rf_hi_e12
            print(f"    HIGH-gain Rf \u2192 {eng(rf_hi_e12):>8s}  "
                  f"(target {args.target_high*100:.0f}% FS, gives {v/args.vref*100:.0f}% "
                  f"= {v:.2f} V)")
            print(f"              Cf \u2248 {1.0/(2*np.pi*rf_hi_e12*200)*1e12:.0f} pF "
                  f"(~200 Hz pole)")
    # headroom / dynamic-range note
    if ipd_bright and ipd_dim and ipd_bright > 0 and ipd_dim > 0:
        dr = ipd_bright / ipd_dim
        print("  " + "\u2500" * 60)
        print(f"  Light dynamic range bright/dim \u2248 {dr:.0f}\u00d7 "
              f"({20*np.log10(dr):.0f} dB).")
        if rf_lo_e12 and rf_hi_e12:
            print(f"  Chosen gains differ by {rf_hi_e12/rf_lo_e12:.0f}\u00d7, so together the")
            print(f"  two channels cover the full bright\u2192dim swing on-scale.")
    print("  " + "\u2550" * 60)

    # ================================================================== #
    #  LIVE dual readout
    # ================================================================== #
    print("\n  LIVE DUAL READOUT (Ctrl+C to stop)")
    print(f"   CH{cl}(low)              CH{chh}(high)")
    try:
        while True:
            a = adc.read(cl); b = adc.read(chh)
            print(f"   {a:5d} {adc.volts(a):.3f}V {bar(a,16)}   "
                  f"{b:5d} {adc.volts(b):.3f}V {bar(b,16)}")
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n  Live view stopped.")
    finally:
        adc.close()


if __name__ == "__main__":
    main()
