#!/usr/bin/env python3
"""
tests/06_capture.py  (or scripts/06_capture.py)
────────────────────────────────────────────────
Shadowbands — timed data capture for the 4× OPT101 array on MCP3208 CH1–CH4.

Records interleaved samples of all channels to CSV, with a JSON metadata
sidecar (channels, rate, UTC anchor, notes, optional orientation fields).

TIMING INPUTS (all optional, combine as needed):
  --start-in  S      wait S seconds before starting (delta to start)
  --start-at  HH:MM[:SS]   start at this UTC wall-clock time today
  --duration  S      record for S seconds
  --end-in    S      stop S seconds from NOW (alternative to --duration)
  --end-at    HH:MM[:SS]   stop at this UTC time
  (no end given → record until Ctrl+C)

OUTPUT INPUTS:
  --name  NAME       base filename (default: shadowbands_YYYYmmdd_HHMMSS)
  --outdir DIR       output directory (default: ./data)
  --notes "text"     free-text note stored in the metadata sidecar

OTHER:
  --chans 1 2 3 4    MCP3208 channels (default 1 2 3 4)
  --rate  200        samples per second per scan (default 200)

Examples:
  # start in 60 s, record 10 minutes, default name/location
  python3 tests/06_capture.py --start-in 60 --duration 600

  # start at 13:44 UTC, end at 13:52 UTC, custom name + folder
  python3 tests/06_capture.py --start-at 13:44 --end-at 13:52 \
        --name totality_run1 --outdir /home/pi/eclipse_data \
        --notes "L-frame X-arm az=3.2deg, tilt 0.4deg, Montejo"

  # record until Ctrl+C
  python3 tests/06_capture.py

CSV columns:
  t_rel_s   seconds since capture start (monotonic, sample spacing truth)
  t_utc     UTC unix timestamp (anchor + t_rel; absolute time reference)
  chA..chD  raw 12-bit counts per channel

Ctrl+C at any point stops cleanly and finalises the files.
"""
import sys, os, time, csv, json, argparse, signal
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config, setup_logging, MCP3208

LABELS = ["A", "B", "C", "D"]


def parse_utc_hhmm(s):
    """'HH:MM' or 'HH:MM:SS' (UTC, today) -> unix timestamp. Rolls to
    tomorrow if that time is already >12 h in the past."""
    parts = s.split(":")
    if len(parts) == 2:
        h, m, sec = int(parts[0]), int(parts[1]), 0
    elif len(parts) == 3:
        h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        raise argparse.ArgumentTypeError(f"bad time '{s}' (use HH:MM or HH:MM:SS)")
    now = datetime.now(timezone.utc)
    t = now.replace(hour=h, minute=m, second=sec, microsecond=0)
    if (now - t).total_seconds() > 12 * 3600:
        t += timedelta(days=1)
    return t.timestamp()


def fmt_hms(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds//3600:02d}:{(seconds//60)%60:02d}:{seconds%60:02d}"


def main():
    ap = argparse.ArgumentParser(description="4x OPT101 array capture")
    # timing
    ap.add_argument("--start-in", type=float, default=None,
                    help="delay in seconds before starting")
    ap.add_argument("--start-at", type=parse_utc_hhmm, default=None,
                    help="UTC start time HH:MM[:SS]")
    ap.add_argument("--duration", type=float, default=None,
                    help="record for this many seconds")
    ap.add_argument("--end-in", type=float, default=None,
                    help="stop this many seconds from NOW")
    ap.add_argument("--end-at", type=parse_utc_hhmm, default=None,
                    help="UTC end time HH:MM[:SS]")
    # output
    ap.add_argument("--name", type=str, default=None,
                    help="base filename (default shadowbands_<UTC stamp>)")
    ap.add_argument("--outdir", type=str, default="data",
                    help="output directory (default ./data)")
    ap.add_argument("--notes", type=str, default="",
                    help="free-text note stored in metadata")
    # acquisition
    ap.add_argument("--chans", type=int, nargs="+", default=[1, 2, 3, 4],
                    help="MCP3208 channels (default 1 2 3 4)")
    ap.add_argument("--rate", type=float, default=200.0,
                    help="scans per second (default 200)")
    args = ap.parse_args()

    if args.start_in is not None and args.start_at is not None:
        ap.error("use --start-in OR --start-at, not both")
    if sum(x is not None for x in (args.duration, args.end_in, args.end_at)) > 1:
        ap.error("use only one of --duration / --end-in / --end-at")

    labels = LABELS[:len(args.chans)] if len(args.chans) <= 4 else \
             [f"S{i}" for i in range(len(args.chans))]

    cfg = load_config()
    setup_logging(cfg)
    hcfg = cfg["hardware"]

    # ---- resolve output paths ----
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = args.name if args.name else f"shadowbands_{stamp}"
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    csv_path = outdir / f"{base}.csv"
    meta_path = outdir / f"{base}.meta.json"
    if csv_path.exists():
        print(f"  ! {csv_path} exists — appending _1/_2… to avoid overwrite")
        k = 1
        while (outdir / f"{base}_{k}.csv").exists():
            k += 1
        base = f"{base}_{k}"
        csv_path = outdir / f"{base}.csv"
        meta_path = outdir / f"{base}.meta.json"

    # ---- resolve start time ----
    now = time.time()
    if args.start_at is not None:
        start_utc = args.start_at
    elif args.start_in is not None:
        start_utc = now + args.start_in
    else:
        start_utc = now

    # ---- resolve end time (None = until Ctrl+C) ----
    if args.duration is not None:
        end_utc = start_utc + args.duration
    elif args.end_in is not None:
        end_utc = now + args.end_in
    elif args.end_at is not None:
        end_utc = args.end_at
    else:
        end_utc = None

    if end_utc is not None and end_utc <= start_utc:
        ap.error("end time is not after start time")

    print("\n╔══════════════════════════════════════════════════╗")
    print("║  06 — Shadowbands array capture                   ║")
    print("╚══════════════════════════════════════════════════╝")
    print("  " + "   ".join(f"{l}→CH{c}" for l, c in zip(labels, args.chans)))
    print(f"  rate      : {args.rate:.0f} scans/s")
    print(f"  output    : {csv_path}")
    print(f"  start UTC : {datetime.fromtimestamp(start_utc, timezone.utc):%H:%M:%S}"
          + (f"  (in {fmt_hms(start_utc-now)})" if start_utc > now else "  (now)"))
    if end_utc:
        print(f"  end   UTC : {datetime.fromtimestamp(end_utc, timezone.utc):%H:%M:%S}"
              f"  (length {fmt_hms(end_utc-start_utc)})")
    else:
        print("  end       : Ctrl+C")
    if args.notes:
        print(f"  notes     : {args.notes}")

    adc = MCP3208(bus=hcfg["spi_bus"], ce=hcfg["spi_ce_mcp3208_1"],
                  hz=hcfg["spi_hz"])

    # graceful Ctrl+C
    stop = {"flag": False}
    def _sig(_a, _b):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    # ---- wait for start (abortable) ----
    while time.time() < start_utc and not stop["flag"]:
        rem = start_utc - time.time()
        print(f"\r  waiting to start… {fmt_hms(rem)}  (Ctrl+C aborts)  ",
              end="", flush=True)
        time.sleep(min(0.5, max(0.05, rem)))
    print()
    if stop["flag"]:
        print("  aborted before start."); adc.close(); return

    # ---- metadata sidecar (written at start, finalised at end) ----
    utc_anchor = time.time()
    mono_anchor = time.perf_counter()
    meta = {
        "project": "Shadowbands",
        "file": csv_path.name,
        "channels": {l: c for l, c in zip(labels, args.chans)},
        "rate_sps": args.rate,
        "vref": 3.3,
        "adc_bits": 12,
        "utc_anchor": utc_anchor,
        "utc_anchor_iso": datetime.fromtimestamp(
            utc_anchor, timezone.utc).isoformat(),
        "planned_end_utc": end_utc,
        "notes": args.notes,
        # fill these from your orientation procedure (Sun fix + tilt):
        "orientation": {
            "x_arm_true_bearing_deg": None,
            "tilt_roll_deg": None,
            "tilt_pitch_deg": None,
            "method": None,
        },
        "status": "recording",
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    # ---- capture loop ----
    period = 1.0 / args.rate
    n = 0
    next_t = mono_anchor
    last_status = 0.0
    print("  recording…  (Ctrl+C to stop)\n")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_rel_s", "t_utc"] + [f"ch{l}" for l in labels])
        try:
            while not stop["flag"]:
                t_rel = time.perf_counter() - mono_anchor
                if end_utc is not None and utc_anchor + t_rel >= end_utc:
                    break
                row = [adc.read(c) for c in args.chans]
                w.writerow([f"{t_rel:.4f}", f"{utc_anchor + t_rel:.4f}"] + row)
                n += 1
                if n % 500 == 0:
                    f.flush()
                # pacing
                next_t += period
                while time.perf_counter() < next_t:
                    pass
                # status line ~1/s
                if t_rel - last_status >= 1.0:
                    last_status = t_rel
                    ach = n / t_rel if t_rel > 0 else 0
                    rem = "" if end_utc is None else \
                          f"  remaining {fmt_hms(end_utc - (utc_anchor+t_rel))}"
                    vals = "  ".join(f"{l}:{v:4d}" for l, v in zip(labels, row))
                    print(f"\r  t={fmt_hms(t_rel)}  n={n}  ~{ach:.0f}sps  "
                          f"{vals}{rem}   ", end="", flush=True)
        finally:
            f.flush()
    print()

    # ---- finalise metadata ----
    t_total = time.perf_counter() - mono_anchor
    meta["status"] = "stopped_by_user" if stop["flag"] else "completed"
    meta["samples"] = n
    meta["actual_duration_s"] = round(t_total, 3)
    meta["achieved_sps"] = round(n / t_total, 2) if t_total > 0 else None
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"  saved {n} scans over {fmt_hms(t_total)} "
          f"(~{n/max(t_total,1e-9):.0f} sps)")
    print(f"  data : {csv_path}")
    print(f"  meta : {meta_path}")
    adc.close()


if __name__ == "__main__":
    main()
