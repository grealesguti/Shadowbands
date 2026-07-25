#!/usr/bin/env python3
"""
tests/03c_opt101_array4_capture.py
───────────────────────────────────
Test 3c — CAPTURE stage. Take real data from the 4× OPT101 array on the
MCP3208 (CH0–CH3) as fast as the SPI/ADC path allows, persist it, and hand
the saved file to the plotting script.

This is the acquisition-only sibling of 03c_..._debug_0ch_3ch.py. It reuses
the SAME hardware bring-up (utils.hw.MCP3208, config.yaml, SPI check) but
DOES NOT repeat any of the debug/diagnostic stages (dark/light, independence,
noise floor, cross-correlation, matching). It just streams samples.

Channels are 0-indexed: CH0→ch0, CH1→ch1, CH2→ch2, CH3→ch3.

Outputs (written next to each other):
  <out>.csv       t_rel_s, t_utc, ch0..ch3   (human-readable, plotter-native)
  <out>.parquet   same data, zstd-compressed, lossless, low footprint

Usage:
  # free-run for 30 s at max speed, then plot to a vector PDF and open it
  python3 tests/03c_opt101_array4_capture.py --seconds 30 --plot --pdf run.pdf --open

  # fixed 200 sps for 8 s, chans 0..3 (default), just save
  python3 tests/03c_opt101_array4_capture.py --seconds 8 --rate 200 --out data/run

  # find the ceiling first (no hardware writes), then capture at max
  python3 tests/03c_opt101_array4_capture.py --probe-rate
  python3 tests/03c_opt101_array4_capture.py --seconds 60          # uncapped

Flags:
  --out PREFIX     output path prefix (default: data/capture_<UTCstamp>)
  --seconds S      capture duration in seconds (default 30)
  --rate HZ        target sample rate; omit / 0 = uncapped (max speed)
  --chans a b c d  four MCP3208 channels (default 0 1 2 3)
  --probe-rate     measure max achievable read rate (2 s) and exit
  --plot           run the plotting script on the saved CSV afterwards
  --pdf / --svg / --jpg PATH   forwarded to the plotter (vector preferred)
  --open           forwarded to the plotter (open the figure after)
  --no-parquet     skip the Parquet re-save (CSV only)
"""
import sys, os, csv, time, argparse, subprocess, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, MCP3208

PLOTTER = Path(__file__).with_name("03c_opt101_array4_plotting.py")


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def open_adc():
    """Same bring-up path as the 03c debug script."""
    cfg = load_config()
    setup_logging(cfg)
    hcfg = cfg["hardware"]
    if not os.path.exists("/dev/spidev0.0"):
        print("  ✗ SPI device /dev/spidev0.0 missing.")
        print("  Enable SPI: sudo raspi-config nonint do_spi 0  (then reboot)")
        sys.exit(1)
    adc = MCP3208(bus=hcfg["spi_bus"],
                  ce=hcfg["spi_ce_mcp3208_1"],
                  hz=hcfg["spi_hz"])
    return adc


def probe_rate(adc, chans, seconds=2.0):
    """Measure the maximum uncapped 4-channel read rate (no persistence)."""
    print(f"\n  Probing max read rate over {seconds:.0f} s ({len(chans)} channels)…")
    n = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        for c in chans:
            adc.read(c)
        n += 1
    dt = time.monotonic() - t0
    sps = n / dt
    per_read = dt / (n * len(chans)) * 1e6
    print(f"  → {n} scans in {dt:.2f} s = {sps:.0f} sps "
          f"({sps*len(chans):.0f} single reads/s, {per_read:.1f} µs/read)")
    return sps


def capture(adc, chans, seconds, rate, csv_path):
    """Stream CH scans to CSV. Returns (t_rel list, dict ch->list, achieved fs).

    Uncapped (rate<=0) runs the read loop flat out. A positive rate paces the
    loop to that target. Time base is monotonic; a wall-clock UTC stamp is
    written per row for absolute alignment during totality.
    """
    ncols = len(chans)
    t_rel, cols = [], [[] for _ in range(ncols)]
    period = (1.0 / rate) if rate and rate > 0 else 0.0

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    utc0 = utc_now_iso()
    print(f"\n  Capturing {seconds:.0f} s "
          f"({'uncapped' if period == 0 else f'{rate:.0f} sps target'}) → {csv_path}")
    print(f"  t0 UTC = {utc0}   (Ctrl+C to stop early)")

    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_rel_s", "t_utc"] + [f"ch{c}" for c in chans])
        try:
            while True:
                tick = time.monotonic()
                trel = tick - t0
                if trel >= seconds:
                    break
                row = [adc.read(c) for c in chans]
                # only stamp UTC each row when paced; uncapped skips it for speed
                utc = utc_now_iso() if period else ""
                w.writerow([f"{trel:.6f}", utc] + row)
                t_rel.append(trel)
                for j in range(ncols):
                    cols[j].append(row[j])
                if period:
                    rem = period - (time.monotonic() - tick)
                    if rem > 0:
                        time.sleep(rem)
        except KeyboardInterrupt:
            print("\n  Capture stopped early.")

    dur = (t_rel[-1] - t_rel[0]) if len(t_rel) > 1 else 0.0
    fs = (len(t_rel) - 1) / dur if dur > 0 else float("nan")
    data = {f"{c}": cols[j] for j, c in enumerate(chans)}
    print(f"  {len(t_rel)} samples   {dur:.1f} s   ~{fs:.0f} sps   → {csv_path}")
    return t_rel, data, fs


def save_parquet(t_rel, data, path):
    """Compact, lossless re-save (zstd). Counts as smallest int; time float64."""
    try:
        import numpy as np
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        print("  ! Parquet skipped: needs numpy+pyarrow "
              "(pip3 install pyarrow --break-system-packages)")
        return None
    arrays = {"t_rel_s": pa.array(np.asarray(t_rel, dtype=np.float64))}
    for name, v in data.items():
        v = np.asarray(v)
        vmax = int(v.max()) if len(v) else 0
        dt = np.uint16 if vmax < 2**16 else (np.uint32 if vmax < 2**32 else np.int64)
        arrays[f"ch{name}"] = pa.array(v.astype(dt))
    pq.write_table(pa.table(arrays), path, compression="zstd", use_dictionary=False)
    sz = Path(path).stat().st_size
    print(f"  Parquet: {path}  ({sz/1024:.1f} KiB, zstd)")
    return path


def run_plotter(csv_path, args):
    """Hand the saved CSV to the plotting script for figures / open."""
    if not PLOTTER.exists():
        print(f"  ! plotter not found: {PLOTTER}")
        return
    cmd = [sys.executable, str(PLOTTER), str(csv_path)]
    if args.pdf:  cmd += ["--pdf", args.pdf]
    if args.svg:  cmd += ["--svg", args.svg]
    if args.jpg:  cmd += ["--jpg", args.jpg]
    if args.open: cmd += ["--open"]
    print(f"\n  → plotting: {' '.join(cmd)}")
    subprocess.run(cmd)


def main():
    ap = argparse.ArgumentParser(description="capture 4× OPT101 (MCP3208 CH0–CH3)")
    ap.add_argument("--out", type=str, default=None,
                    help="output path prefix (default data/capture_<UTC>)")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--rate", type=float, default=0.0,
                    help="target sps; 0 = uncapped (max speed)")
    ap.add_argument("--chans", type=int, nargs=4, default=[0, 1, 2, 3])
    ap.add_argument("--probe-rate", action="store_true",
                    help="measure max read rate (2 s) and exit")
    ap.add_argument("--plot", action="store_true",
                    help="plot the saved CSV afterwards")
    ap.add_argument("--pdf", type=str, default=None)
    ap.add_argument("--svg", type=str, default=None)
    ap.add_argument("--jpg", type=str, default=None)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--no-parquet", action="store_true")
    args = ap.parse_args()

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  Test 3c — 4× OPT101 CAPTURE (no debug stages)    ║")
    print("╚══════════════════════════════════════════════════╝")
    print("  " + "   ".join(f"CH{c}→ch{c}" for c in args.chans))

    adc = open_adc()
    try:
        if args.probe_rate:
            probe_rate(adc, args.chans)
            return

        if args.out:
            prefix = Path(args.out)
        else:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            prefix = Path("data") / f"capture_{stamp}"
        csv_path = prefix.with_suffix(".csv")

        t_rel, data, fs = capture(adc, args.chans, args.seconds, args.rate, csv_path)

        if len(t_rel) < 2:
            print("  ! no data captured; nothing to save/plot.")
            return

        if not args.no_parquet:
            save_parquet(t_rel, data, prefix.with_suffix(".parquet"))

        # plot if the user asked, or if any figure output was requested
        if args.plot or args.pdf or args.svg or args.jpg or args.open:
            run_plotter(csv_path, args)
    finally:
        adc.close()


if __name__ == "__main__":
    main()
