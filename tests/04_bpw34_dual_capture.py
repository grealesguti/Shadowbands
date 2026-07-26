#!/usr/bin/env python3
"""
tests/04_bpw34_dual_capture.py
──────────────────────────────
Test 4 — DUAL-BPW34 / dual-gain intensity CAPTURE. Independent async logger
that streams both transimpedance channels of the BPW34 + MCP6002 front-end as
fast as SPI allows (or at a pinned rate), persists to CSV + Parquet, and hands
the file to a plotter. Sibling in style to 04_as7343_spectral_capture.py, but a
DIFFERENT acquisition model:

  AS7343 path : digital I²C, self-timed, ~tens of ms per 14-channel frame.
  BPW34 path  : two analog TIAs → MCP3208 → SPI. Each conversion is a
                microsecond bus read, so a 2-channel frame is fast (~kHz
                achievable). This is the FAST intensity time-series the AS7343
                cannot give you — its whole point is temporal shadow-band
                structure, not wavelength.

Two BPW34 photodiodes / two gains (see the TikZ diagram PDF that ships with the
detector docs):
  CH_LOW  ← amp A, LOW gain  (Rf_A, bright / normal light — won't saturate)
  CH_HIGH ← amp B, HIGH gain (Rf_B, dim / near-totality — shadow-band contrast)

Each frame logs BOTH channels plus their volts and the back-calculated
photocurrent, so in post you can:
  * use LOW while it's bright, HIGH once it dims, and stitch a single
    calibrated irradiance track across the whole event, and
  * ask "does the shadow-band contrast survive into the dim, high-gain band?"

Like the AS7343 logger, this runs as its OWN process and UTC-stamps every row
(RTC/NTP keeps multiple Pis coherent) so it aligns in post with the spectral
stream and any other OPT101/BPW34 nodes.

CRITICAL for the eclipse: LOCK the gains (the fitted Rf on each amp) and the
SPI/sample settings before totality, and RECORD them in the file header. You
cannot change a soldered Rf mid-event, but you CAN accidentally change the
sample rate or SPI clock between a rehearsal and the real run — the
--lock-config gate refuses to start a real capture unless the acquisition
config is pinned, mirroring the AS7343 gain/integration lock.

Wiring (Stage-0/1 dual-gain build):
  Pi 3V3        → MCP3208 VDD/VREF, MCP6002 V+           (3.3 V analog rail)
  Pi GND        → MCP3208 AGND/DGND, MCP6002 V-, BPW34 anodes
  Pi SCLK  (23) → MCP3208 CLK (13)
  Pi MOSI  (19) → MCP3208 DIN (11)
  Pi MISO  (21) → MCP3208 DOUT (12)
  Pi CE0   (24) → MCP3208 /CS (10)
  BPW34-A cathode → MCP6002 pin2 (−A) summing node;  amp A out (pin1) → CH_LOW
  BPW34-B cathode → MCP6002 pin6 (−B) summing node;  amp B out (pin7) → CH_HIGH
  Shared Vref (~0.14 V divider) → both + inputs (pin3, pin5)

Dependencies:
  spidev   (pip3 install spidev  --break-system-packages)
  numpy + pyarrow for the Parquet re-save (optional)

Usage:
  # probe the achievable 2-channel sample rate (no writes) and exit
  python3 tests/04_bpw34_dual_capture.py --probe-rate

  # 60 s at a pinned 500 sps/channel, gains recorded, locked
  python3 tests/04_bpw34_dual_capture.py \
      --seconds 60 --sample-rate 500 --rf-low 100e3 --rf-high 2.2e6 \
      --lock-config --out data/bpw34_run

  # free-run as fast as SPI allows for 30 s (rehearsal; still needs the lock
  # for a "real" capture)
  python3 tests/04_bpw34_dual_capture.py --seconds 30 --sample-rate 0 --lock-config

Flags mirror the AS7343 capture where it makes sense (--out/--seconds/--plot/
--pdf/--svg/--jpg/--open/--no-parquet/--probe-rate/--lock-config) so the two
feel like siblings.
"""
import sys, os, csv, time, argparse, subprocess, datetime
from pathlib import Path

try:
    import spidev
    HAVE_SPIDEV = True
except ImportError:
    HAVE_SPIDEV = False

PLOTTER = Path(__file__).with_name("04_bpw34_dual_plotting.py")

# ── frame layout ────────────────────────────────────────────────
# The two raw intensity channels plus per-channel derived volts and the
# back-calculated photocurrent (nA). CLEAR-like "best" column picks whichever
# channel is on-scale (HIGH if not saturated, else LOW) as a convenience track.
CH_NAMES = ["LOW", "HIGH", "LOW_V", "HIGH_V", "LOW_nA", "HIGH_nA", "BEST_nA"]

SAT_COUNTS = 4000        # ≥ this ⇒ treat channel as saturated / near-rail
FLOOR_COUNTS = 6         # ≤ this ⇒ treat channel as pinned low


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


class MCP3208:
    """Minimal single-ended reader for the MCP3208 12-bit ADC on SPI0.

    Only the paths this logger needs: open, a fast single-ended read, a
    counts→volts helper, and close. Mirrors the driver used by the bring-up /
    calibrate script so behaviour is identical across tools.
    """
    def __init__(self, bus=0, dev=0, hz=1_000_000, vref=3.3):
        if not HAVE_SPIDEV:
            raise RuntimeError("spidev missing: pip3 install spidev --break-system-packages")
        self.vref = vref
        self.spi = spidev.SpiDev()
        self.spi.open(bus, dev)
        self.spi.max_speed_hz = hz
        self.spi.mode = 0

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


class DualBPW34:
    """Two-channel dual-gain front-end reader.

    Wraps one MCP3208 and knows which channel is the LOW-gain (bright) amp and
    which is the HIGH-gain (dim) amp, plus the fitted feedback resistors and the
    shared Vref bias, so it can turn raw counts into photocurrent on the fly.
    """
    def __init__(self, adc, ch_low, ch_high, rf_low, rf_high, vbias):
        self.adc = adc
        self.ch_low = ch_low
        self.ch_high = ch_high
        self.rf_low = rf_low
        self.rf_high = rf_high
        self.vbias = vbias

    def _ipd(self, counts, rf):
        """Photocurrent (A) from a TIA reading: I_pd = (Vout − Vbias) / Rf."""
        return (self.adc.volts(counts) - self.vbias) / rf

    def read_frame(self):
        """Read both channels once → dict matching CH_NAMES.

        BEST_nA prefers the HIGH-gain estimate (lower noise per nA) unless that
        channel is saturated, in which case it falls back to the LOW-gain
        estimate — the stitching rule you want across a darkening sky.
        """
        lo = self.adc.read(self.ch_low)
        hi = self.adc.read(self.ch_high)
        ipd_lo = self._ipd(lo, self.rf_low)
        ipd_hi = self._ipd(hi, self.rf_high)
        best = ipd_lo if hi >= SAT_COUNTS else ipd_hi
        return {
            "LOW": lo,
            "HIGH": hi,
            "LOW_V": self.adc.volts(lo),
            "HIGH_V": self.adc.volts(hi),
            "LOW_nA": ipd_lo * 1e9,
            "HIGH_nA": ipd_hi * 1e9,
            "BEST_nA": best * 1e9,
        }

    def close(self):
        self.adc.close()


# ── acquisition ─────────────────────────────────────────────────
def probe_rate(dev, seconds=3.0):
    print(f"\n  Probing 2-channel sample rate over {seconds:.0f} s…")
    n, t0 = 0, time.monotonic()
    while time.monotonic() - t0 < seconds:
        dev.read_frame()
        n += 1
    dt = time.monotonic() - t0
    sps = n / dt
    print(f"  → {n} frames in {dt:.2f} s = {sps:.0f} frames/s per pair "
          f"({1000.0/sps:.2f} ms/frame, {2*sps:.0f} ADC reads/s)")
    if sps < 500:
        print("  Note: below 500 sps/channel. Lower --hz SPI load, close other")
        print("        processes, or accept it — shadow bands (~4.5 Hz) are still")
        print("        well oversampled even at ~100 sps.")
    return sps


def capture(dev, seconds, target_sps, csv_path, meta):
    """Stream both channels for `seconds`, UTC-stamped, into csv_path.

    target_sps > 0 : pace each frame to ~target_sps per channel (pinned rate).
    target_sps == 0: free-run as fast as SPI allows.
    A commented metadata header block records the locked acquisition config so
    the file is self-describing for post-processing and the eclipse archive.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    period = (1.0 / target_sps) if target_sps > 0 else 0.0
    t0 = time.monotonic()
    utc0 = utc_now_iso()
    print(f"\n  Capturing {seconds:.0f} s → {csv_path}")
    print(f"  t0 UTC = {utc0}   (Ctrl+C to stop early)")
    t_rel, rows = [], []
    with open(csv_path, "w", newline="") as f:
        # self-describing header: locked config, so a rehearsal file and a
        # totality file are never silently different.
        for k, v in meta.items():
            f.write(f"# {k}: {v}\n")
        f.write(f"# t0_utc: {utc0}\n")
        w = csv.writer(f)
        w.writerow(["t_rel_s", "t_utc"] + CH_NAMES)
        nxt = t0
        try:
            while True:
                tick = time.monotonic()
                trel = tick - t0
                if trel >= seconds:
                    break
                fr = dev.read_frame()
                row = [fr[name] for name in CH_NAMES]
                w.writerow([f"{trel:.6f}", utc_now_iso()]
                           + [f"{x:.6g}" if isinstance(x, float) else x for x in row])
                t_rel.append(trel); rows.append(row)
                if period:
                    # pace to the target rate; busy-wait the small remainder
                    nxt += period
                    while time.monotonic() < nxt:
                        pass
        except KeyboardInterrupt:
            print("\n  Capture stopped early.")
    dur = (t_rel[-1] - t_rel[0]) if len(t_rel) > 1 else 0.0
    sps = (len(t_rel) - 1) / dur if dur > 0 else float("nan")
    print(f"  {len(t_rel)} frames   {dur:.1f} s   ~{sps:.0f} sps/ch   → {csv_path}")
    # quick on-scale accounting so you know which channel carried the run
    if rows:
        los = [r[0] for r in rows]; his = [r[1] for r in rows]
        lo_sat = sum(1 for c in los if c >= SAT_COUNTS)
        hi_sat = sum(1 for c in his if c >= SAT_COUNTS)
        print(f"  on-scale: LOW saturated {100*lo_sat/len(rows):.0f}% of frames, "
              f"HIGH saturated {100*hi_sat/len(rows):.0f}%")
    return t_rel, rows, sps


def save_parquet(t_rel, rows, path):
    try:
        import numpy as np, pyarrow as pa, pyarrow.parquet as pq
    except ImportError:
        print("  ! Parquet skipped: needs numpy+pyarrow "
              "(pip3 install pyarrow --break-system-packages)")
        return None
    arr = {"t_rel_s": pa.array(np.asarray(t_rel, dtype=np.float64))}
    cols = list(zip(*rows)) if rows else [[] for _ in CH_NAMES]
    for name, col in zip(CH_NAMES, cols):
        v = np.asarray(col, dtype=np.float64)
        arr[name] = pa.array(v)
    pq.write_table(pa.table(arr), path, compression="zstd", use_dictionary=False)
    print(f"  Parquet: {path}  ({Path(path).stat().st_size/1024:.1f} KiB, zstd)")
    return path


def run_plotter(csv_path, args):
    if not PLOTTER.exists():
        print(f"  ! plotter not found: {PLOTTER} (skipping)")
        return
    cmd = [sys.executable, str(PLOTTER), str(csv_path)]
    if args.pdf:  cmd += ["--pdf", args.pdf]
    if args.svg:  cmd += ["--svg", args.svg]
    if args.jpg:  cmd += ["--jpg", args.jpg]
    if args.open: cmd += ["--open"]
    print(f"\n  → plotting: {' '.join(cmd)}")
    subprocess.run(cmd)


def main():
    ap = argparse.ArgumentParser(description="capture dual-gain BPW34 intensity data")
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--ch-low", type=int, default=0,
                    help="MCP3208 channel for the LOW-gain (bright) amp (default 0)")
    ap.add_argument("--ch-high", type=int, default=1,
                    help="MCP3208 channel for the HIGH-gain (dim) amp (default 1)")
    ap.add_argument("--rf-low", type=float, default=100e3,
                    help="Rf fitted on the LOW-gain amp, ohms (recorded; LOCK before totality)")
    ap.add_argument("--rf-high", type=float, default=2.2e6,
                    help="Rf fitted on the HIGH-gain amp, ohms (recorded; LOCK before totality)")
    ap.add_argument("--vref-bias", type=float, default=0.14,
                    help="shared non-inverting bias volts (default 0.14)")
    ap.add_argument("--sample-rate", type=int, default=500,
                    help="target sps PER CHANNEL; 0 = free-run as fast as SPI allows")
    ap.add_argument("--bus", type=int, default=0, help="SPI bus (Pi = 0)")
    ap.add_argument("--dev", type=int, default=0, help="SPI device / CE (default 0)")
    ap.add_argument("--hz", type=int, default=1_000_000, help="SPI clock Hz")
    ap.add_argument("--vref", type=float, default=3.3, help="ADC reference volts")
    ap.add_argument("--lock-config", action="store_true",
                    help="required to run a real capture: asserts acquisition config is pinned")
    ap.add_argument("--probe-rate", action="store_true")
    ap.add_argument("--plot", action="store_true")
    ap.add_argument("--pdf", type=str, default=None)
    ap.add_argument("--svg", type=str, default=None)
    ap.add_argument("--jpg", type=str, default=None)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--no-parquet", action="store_true")
    args = ap.parse_args()

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 4 — DUAL-BPW34 dual-gain intensity capture  ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"  CH_LOW=CH{args.ch_low} (Rf {args.rf_low:.3g}Ω)   "
          f"CH_HIGH=CH{args.ch_high} (Rf {args.rf_high:.3g}Ω)")
    print(f"  Vbias={args.vref_bias} V   "
          f"rate={'free-run' if args.sample_rate == 0 else str(args.sample_rate)+' sps/ch'}   "
          f"SPI={args.hz} Hz")

    if not HAVE_SPIDEV:
        print("\n  ✗ spidev missing. pip3 install spidev --break-system-packages")
        sys.exit(1)

    dev_path = f"/dev/spidev{args.bus}.{args.dev}"
    if not os.path.exists(dev_path):
        print(f"\n  ✗ {dev_path} missing — enable SPI (raspi-config) / check --bus/--dev.")
        sys.exit(1)

    adc = MCP3208(bus=args.bus, dev=args.dev, hz=args.hz, vref=args.vref)
    dev = DualBPW34(adc, args.ch_low, args.ch_high,
                    args.rf_low, args.rf_high, args.vref_bias)
    try:
        # sanity: both channels responding and in range
        f0 = dev.read_frame()
        print(f"  first frame: LOW={f0['LOW']} ({f0['LOW_V']:.3f} V)   "
              f"HIGH={f0['HIGH']} ({f0['HIGH_V']:.3f} V)")
        for nm, c in (("LOW", f0["LOW"]), ("HIGH", f0["HIGH"])):
            if c <= FLOOR_COUNTS or c >= 4090:
                print(f"  ! {nm} channel pinned ({c} counts) — check wiring/osc before capturing.")

        if args.probe_rate:
            probe_rate(dev)
            return

        if not args.lock_config:
            print("\n  ✗ Refusing to capture without --lock-config.")
            print("    Pin --sample-rate, --hz and the fitted --rf-low/--rf-high, then pass")
            print("    --lock-config so a rehearsal and the totality run can't silently")
            print("    differ in acquisition config. (Probe rate is allowed without the lock.)")
            sys.exit(2)

        if args.out:
            prefix = Path(args.out)
        else:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            prefix = Path("data") / f"bpw34_{stamp}"
        csv_path = prefix.with_suffix(".csv")

        # locked, self-describing metadata written into the CSV header
        meta = {
            "sensor": "BPW34 x2 dual-gain via MCP6002 + MCP3208",
            "ch_low": args.ch_low, "ch_high": args.ch_high,
            "rf_low_ohm": f"{args.rf_low:.6g}", "rf_high_ohm": f"{args.rf_high:.6g}",
            "vref_bias_v": args.vref_bias, "adc_vref_v": args.vref,
            "spi_hz": args.hz, "target_sps_per_ch": args.sample_rate,
            "sat_counts": SAT_COUNTS,
        }

        t_rel, rows, sps = capture(dev, args.seconds, args.sample_rate, csv_path, meta)
        if len(t_rel) < 2:
            print("  ! no data captured.")
            return
        if not args.no_parquet:
            save_parquet(t_rel, rows, prefix.with_suffix(".parquet"))
        if args.plot or args.pdf or args.svg or args.jpg or args.open:
            run_plotter(csv_path, args)
    finally:
        dev.close()


if __name__ == "__main__":
    main()
