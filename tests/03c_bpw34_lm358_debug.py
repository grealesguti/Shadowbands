#!/usr/bin/env python3
"""
03c_bpw34_lm358_debug.py  --  Stage-0 bring-up & circuit debug for the
                              discrete BPW34 + LM358 transimpedance channel.

Reads one MCP3208 single-ended channel (default CH0) over SPI0/CE0 and walks a
series of staged checks, printing PASS / FAIL / WARN with hints specific to the
discrete front-end.

Wiring assumed (Stage-0, minimal build):
    BPW34 cathode -> LM358 pin 2 (IN1-) summing node  (anode -> GND)
    LM358 pin 1 (OUT1) -> direct wire -> MCP3208 CH0   (no anti-alias RC yet)
    Rf = 1 MOhm from pin 1 to pin 2,  NO Cf yet
    LM358 pin 3 (IN1+) -> GND     (bias reference = 0 V)
    LM358 pin 4 (V-)   -> GND
    LM358 pin 8 (V+)   -> +3.3 V
    2nd op-amp unused: pin 6 <-> pin 7, pin 5 -> GND
    MCP3208 VDD/VREF -> +3.3 V, AGND/DGND -> GND

Expected Stage-0 behaviour:
    - Dark  : output sits near the LM358 output floor (a few tens of mV,
              NOT true 0 V, because pin 3 is grounded).
    - Light : output RISES; bright room light will likely SATURATE near the
              LM358 single-supply ceiling (~1.7-1.9 V on a 3.3 V rail).
    - If light makes it go the WRONG way (or it sits pinned near 0 and won't
      move) the BPW34 is almost certainly reversed -> swap its two leads.

Run:   sudo python3 tests/03c_bpw34_lm358_debug.py [--channel 0]
"""

import sys
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
    print("\u2554" + "\u2550" * 52 + "\u2557")
    print("\u2551  03c \u2014 BPW34 + LM358 bring-up & circuit debug   \u2551")
    print("\u255a" + "\u2550" * 52 + "\u255d")
    print()
    print("  BPW34 cathode \u2192 LM358 pin2 (Rf junction) \u2192 pin1 \u2192 CH0")
    print("  Stage-0: direct wire (no anti-alias RC), Rf=1M, no Cf")
    print()


def ok(desc, detail=""):
    print(f"  \u2713 [PASS]  {desc:<42s} {detail}")


def bad(desc, detail=""):
    print(f"  \u2717 [FAIL]  {desc:<42s} {detail}")


def warn(desc, detail=""):
    print(f"  ! [WARN]  {desc:<42s} {detail}")


def bar(counts, width=36):
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


def timed(adc, ch, target_sps, seconds):
    """Sample at approx target_sps for `seconds`; return (array, achieved_sps)."""
    period = 1.0 / target_sps if target_sps > 0 else 0.0
    n = max(1, int(target_sps * seconds))
    buf = np.empty(n, dtype=np.int32)
    t0 = time.perf_counter()
    nxt = t0
    for i in range(n):
        buf[i] = adc.read(ch)
        nxt += period
        while time.perf_counter() < nxt:
            pass
    dt = time.perf_counter() - t0
    return buf, (n / dt if dt > 0 else float("nan"))


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


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=0, help="MCP3208 channel (default 0)")
    ap.add_argument("--vref", type=float, default=3.3, help="ADC reference volts")
    ap.add_argument("--hz", type=int, default=1_000_000, help="SPI clock Hz")
    args = ap.parse_args()
    ch = args.channel

    hdr()

    # ---- SPI present -----------------------------------------------------
    import os
    dev_path = "/dev/spidev0.0"
    if os.path.exists(dev_path):
        ok("SPI device present", dev_path)
    else:
        bad("SPI device present", f"{dev_path} missing \u2014 enable SPI (raspi-config)")
        return

    adc = MCP3208(hz=args.hz, vref=args.vref)
    passes = 0
    total = 6

    # ================================================================== #
    print("\n  STAGE 1 \u2014 SLOW SINGLE READS (1/s, watch stability)")
    reads = []
    for i in range(8):
        c = adc.read(ch)
        reads.append(c)
        print(f"  {i:<3d}   {c:5d}   {adc.volts(c):.3f}V   {bar(c)}")
        time.sleep(1.0)
    reads = np.array(reads)
    mean1 = reads.mean()
    if 5 <= mean1 <= 4090:
        ok("Reading in ADC range", f"{mean1:.0f} counts ({adc.volts(mean1):.3f} V)")
        passes += 1
    else:
        bad("Reading in ADC range",
            f"{mean1:.0f} counts \u2014 pinned at a rail; check wiring/oscillation")

    # ================================================================== #
    print("\n  STAGE 2 \u2014 DARK / LIGHT RESPONSE (polarity + gain check)")
    input("  \u2192 COVER the BPW34 completely, then press Enter\u2026")
    dark, _ = burst(adc, ch, 400)
    dmean = dark.mean()
    print(f"    Dark:   {dmean:7.1f} counts ({adc.volts(dmean)*1000:6.1f} mV)")
    input("  \u2192 Now EXPOSE it to room light / a lamp, then press Enter\u2026")
    light, _ = burst(adc, ch, 400)
    lmean = light.mean()
    print(f"    Light:  {lmean:7.1f} counts ({adc.volts(lmean)*1000:6.1f} mV)")

    delta = lmean - dmean
    if delta > 100:
        ok("Output rises with light", f"\u0394 = +{delta:.0f} counts")
        passes += 1
    elif delta < -50:
        bad("Output rises with light", f"\u0394 = {delta:.0f} counts \u2014 WRONG WAY")
        print("    \u2192 BPW34 is reversed. Swap its leads: CATHODE must go to")
        print("      the summing node (LM358 pin 2 / Rf), ANODE to GND.")
    else:
        bad("Output rises with light", f"\u0394 = {delta:.0f} counts \u2014 no response")
        print("    \u2192 Check: 4-5 strap? no \u2014 that's OPT101. Here check Rf 1M is")
        print("      in place, pin 3 grounded, and the BPW34 not reversed/open.")

    # dark-floor sanity
    if dmean > 0.8 * 4095:
        warn("Dark level pinned HIGH",
             f"{adc.volts(dmean)*1000:.0f} mV \u2014 reversed diode or LM358 latched")
    elif dmean > 400:
        warn("Dark level a bit high",
             f"{adc.volts(dmean)*1000:.0f} mV \u2014 light leak or big offset")
    if lmean > 0.9 * 4095:
        print("    (Light channel is SATURATED \u2014 expected with Rf=1M in room "
              "light;\n     this is fine, it comes on-scale only in dim / near-totality "
              "light.)")

    # ================================================================== #
    print("\n  STAGE 3 \u2014 NOISE FLOOR & OSCILLATION (hold light constant)")
    quiet, sps_q = burst(adc, ch, 1024)
    qstd = quiet.std()
    qpp = int(quiet.max() - quiet.min())
    print(f"    n=1024  mean={quiet.mean():.1f}  std={qstd:.2f}  "
          f"p-p={qpp}  (@~{sps_q:.0f} sps)")
    if qstd < 4:
        ok("Noise std < 4 counts", f"std={qstd:.2f}")
        passes += 1
    elif qpp > 800:
        bad("Noise std < 4 counts",
            f"std={qstd:.1f}, p-p={qpp} \u2014 looks like OSCILLATION")
        print("    \u2192 LM358 + BPW34 (~70 pF) with 1M and NO Cf can ring/oscillate.")
        print("      Add a small feedback cap Cf across Rf (pin1\u2013pin2), ~3\u201310 pF")
        print("      ceramic. A short wire-loop across Rf (~1 pF) is a quick test.")
    else:
        warn("Noise std < 4 counts", f"std={qstd:.1f} \u2014 higher than ideal")

    # ================================================================== #
    print("\n  STAGE 4 \u2014 SAMPLE-RATE SWEEP (keep light constant!)")
    print("   target  achieved     mean     std")
    base_mean = None
    max_mean = None
    for tgt in (50, 100, 250, 500, 1000, 2000):
        b, ach = timed(adc, ch, tgt, 0.6)
        if base_mean is None:
            base_mean = b.mean()
        max_mean = b.mean()
        print(f"   {tgt:6d}   {ach:7.0f}   {b.mean():7.1f}   {b.std():5.2f}")
    fast, sps_max = burst(adc, ch, 4000)
    print(f"      max   {sps_max:7.0f}   {fast.mean():7.1f}   {fast.std():5.2f}")
    droop = (max_mean - base_mean)
    if abs(droop) < 6:
        ok("No mean droop vs rate", f"\u0394(50\u2192fast) = {droop:+.1f} counts")
        passes += 1
    else:
        bad("No mean droop vs rate",
            f"\u0394 = {droop:+.1f} counts \u2014 source impedance / settling issue")
    if sps_max >= 400:
        pass  # informational
    else:
        warn("Max rate low", f"{sps_max:.0f} sps")

    # ================================================================== #
    print("\n  STAGE 5 \u2014 DYNAMIC RESPONSE (the 4.5 Hz question)")
    input("  \u2192 WAVE fingers over the sensor ~4\u20135/s for 6 s, press Enter to start\u2026")
    dyn, fs = timed(adc, ch, 200, 6.0)
    dpp = int(dyn.max() - dyn.min())
    fpk, _ = dominant_freq(dyn, fs)
    print(f"    n={len(dyn)} @ ~{fs:.0f} sps   mean={dyn.mean():.0f}   "
          f"p-p swing={dpp} counts")
    if fpk:
        print(f"    Dominant modulation: {fpk:.2f} Hz "
              f"(shadowbands \u2248 4.5 Hz; band 0.5\u201320 Hz)")
    if dpp > 100:
        ok("Sees hand-wave modulation", f"p-p={dpp}")
        passes += 1
    else:
        bad("Sees hand-wave modulation", f"p-p={dpp} \u2014 too small")
        print("    \u2192 If Stage 2 passed but this is flat, the channel may be")
        print("      saturated (too bright \u2014 dim the light) or the output is")
        print("      railing. With no RC filter a real wave should swing hard.")

    # ================================================================== #
    print("\n  " + "\u2500" * 54)
    print(f"  {passes}/{total} stages passed")
    if passes < total:
        print("  \u2717 Fix flagged stages before duplicating to a 2nd channel.")
    else:
        print("  \u2713 Channel healthy \u2014 add anti-alias RC next, then CH1.")

    # ================================================================== #
    print("\n  STAGE 6 \u2014 LIVE READOUT (Ctrl+C to stop)")
    print("   counts    volts  level")
    try:
        while True:
            c = adc.read(ch)
            print(f"   {c:5d}   {adc.volts(c):.3f}V  {bar(c)}")
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n  Live view stopped.")
    finally:
        adc.close()


if __name__ == "__main__":
    main()
