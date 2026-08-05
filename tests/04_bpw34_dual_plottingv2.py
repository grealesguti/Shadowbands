#!/usr/bin/env python3
"""
tests/04_bpw34_dual_plotting.py
───────────────────────────────
Plotter for Test 4 — DUAL-BPW34 dual-gain intensity capture.

Reads the CSV written by 04_bpw34_dual_capture.py (which has a commented
"# key: value" metadata header followed by a normal CSV table) and produces
ONE figure PER CHANNEL — not a single combined plot — so that during a long
constant capture you can drop the files somewhere and eyeball each channel on
its own without them fighting for a shared axis. Each figure is saved to JPG
and/or PDF (PDF is vector = crisp when you zoom into shadow-band ripple later).

What gets plotted, one file each:
  LOW_counts   raw LOW-gain ADC counts vs time
  HIGH_counts  raw HIGH-gain ADC counts vs time
  LOW_nA       LOW-gain back-calculated photocurrent (nA) vs time
  HIGH_nA      HIGH-gain back-calculated photocurrent (nA) vs time
  BEST_nA      stitched best-on-scale photocurrent (nA) vs time

Saturation / floor guides (from the header, if present) are drawn on the raw
count plots so a glance tells you whether a channel was railed.

This is intentionally dependency-light: matplotlib + the stdlib csv reader.
numpy is used if present but not required.

Usage (normally invoked by the capture script, but standalone too):
  python3 tests/04_bpw34_dual_plotting.py data/bpw34_run.csv --jpg data/bpw34_run --pdf data/bpw34_run
  python3 tests/04_bpw34_dual_plotting.py data/bpw34_run.csv --jpg data/bpw34_run --open

  --jpg PREFIX   write <PREFIX>_<channel>.jpg  for every channel
  --pdf PREFIX   write <PREFIX>_<channel>.pdf  for every channel
  --svg PREFIX   write <PREFIX>_<channel>.svg  for every channel
  --open         try to open the generated files with the OS viewer
  --dpi N        raster DPI for JPG (default 150)

If NO output flag is given, it defaults to writing JPGs next to the CSV
(<csvstem>_<channel>.jpg) so a bare call still leaves you something to look at.
"""
import sys, csv, argparse, subprocess, platform
from pathlib import Path

import matplotlib
matplotlib.use("Agg")           # headless: safe on a Pi over SSH with no display
import matplotlib.pyplot as plt


# Channels to emit as individual figures: (csv_column, y-label, kind)
#   kind "counts" gets the saturation/floor guide lines; "nA" is a plain trace.
PLOTS = [
    ("LOW",     "LOW-gain ADC counts",           "counts"),
    ("HIGH",    "HIGH-gain ADC counts",          "counts"),
    ("LOW_nA",  "LOW-gain photocurrent  (nA)",   "nA"),
    ("HIGH_nA", "HIGH-gain photocurrent (nA)",   "nA"),
    ("BEST_nA", "BEST (stitched) photocurrent (nA)", "nA"),
]


def read_capture(csv_path):
    """Parse the capture CSV → (meta dict, header list, columns dict of lists).

    The file starts with zero or more '# key: value' comment lines (metadata),
    then a normal CSV with a header row. Rows are floats where possible.
    """
    meta, header, cols = {}, None, {}
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        for raw in reader:
            if not raw:
                continue
            first = raw[0]
            if first.startswith("#"):
                # metadata line: "# key: value" (value may contain ':' — split once)
                line = first.lstrip("#").strip()
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
                continue
            if header is None:
                header = raw
                cols = {name: [] for name in header}
                continue
            for name, val in zip(header, raw):
                try:
                    cols[name].append(float(val))
                except ValueError:
                    cols[name].append(val)   # e.g. the ISO t_utc string
    if header is None:
        raise ValueError(f"no CSV table found in {csv_path}")
    return meta, header, cols


def _guide_lines(ax, meta):
    """Draw saturation/floor guide lines on a raw-counts axis, if we know them."""
    def _as_int(key):
        try:
            return int(float(meta[key]))
        except (KeyError, ValueError):
            return None
    sat = _as_int("sat_counts")
    floor = _as_int("floor_counts")
    ax.axhline(4095, color="0.6", lw=0.8, ls=":", label="ADC full-scale (4095)")
    if sat is not None:
        ax.axhline(sat, color="tab:red", lw=0.9, ls="--", label=f"saturation ({sat})")
    if floor is not None:
        ax.axhline(floor, color="tab:blue", lw=0.9, ls="--", label=f"floor ({floor})")


def make_figures(csv_path, meta, cols, out_flags, dpi):
    """Create and save one figure per channel. Returns the list of saved paths."""
    t = cols.get("t_rel_s")
    if not t:
        raise ValueError("column 't_rel_s' missing — is this a capture CSV?")

    # a compact provenance string for the figure subtitle, from the header
    rf_lo = meta.get("rf_low_ohm", "?"); rf_hi = meta.get("rf_high_ohm", "?")
    sps   = meta.get("target_sps_per_ch", "?"); utc0 = meta.get("t0_utc", "?")
    subtitle = (f"Rf_low={rf_lo}Ω  Rf_high={rf_hi}Ω  "
                f"target={sps} sps/ch  t0={utc0}")

    saved = []
    for col, ylabel, kind in PLOTS:
        if col not in cols:
            print(f"  ! column {col} not in CSV — skipping its plot")
            continue
        y = cols[col]
        # guard: skip a column that is entirely non-numeric / empty
        if not y or not all(isinstance(v, float) for v in y):
            print(f"  ! column {col} not numeric — skipping")
            continue

        fig, ax = plt.subplots(figsize=(11, 4.4))
        ax.plot(t, y, lw=0.7, color="tab:green" if "nA" in col else "tab:purple")
        ax.set_xlabel("t_rel  (s)")
        ax.set_ylabel(ylabel)
        # Title on the figure, provenance subtitle just under it — no overlap.
        fig.suptitle(f"BPW34 dual-gain  —  {col}", fontsize=12,
                     fontweight="bold", x=0.01, ha="left", y=0.985)
        ax.set_title(subtitle, fontsize=7.5, color="0.35", loc="left", pad=6)
        ax.grid(True, lw=0.3, alpha=0.5)
        ax.margins(x=0.01)

        if kind == "counts":
            _guide_lines(ax, meta)
            ax.set_ylim(-50, 4200)
            ax.legend(fontsize=7, loc="upper right", framealpha=0.9)

        # simple stats box (min/median/max) so a saved sheet is self-contained
        try:
            ys = sorted(y)
            med = ys[len(ys)//2]
            stats = f"min {min(y):.4g}   med {med:.4g}   max {max(y):.4g}   N={len(y)}"
            ax.text(0.995, 0.02, stats, transform=ax.transAxes, ha="right",
                    fontsize=7.5, color="0.25",
                    bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="0.8", lw=0.5))
        except ValueError:
            pass

        fig.tight_layout(rect=[0, 0, 1, 0.95])
        for ext, prefix in out_flags:
            path = Path(f"{prefix}_{col}.{ext}")
            path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(path, dpi=dpi if ext in ("jpg", "jpeg", "png") else None,
                        bbox_inches="tight")
            saved.append(path)
            print(f"  saved {path}")
        plt.close(fig)
    return saved


def open_files(paths):
    system = platform.system()
    opener = {"Darwin": "open", "Windows": "start", "Linux": "xdg-open"}.get(system)
    if not opener:
        return
    for p in paths:
        try:
            if system == "Windows":
                subprocess.run(["cmd", "/c", "start", "", str(p)], check=False)
            else:
                subprocess.run([opener, str(p)], check=False)
        except Exception as e:
            print(f"  ! could not open {p}: {e}")


def main():
    ap = argparse.ArgumentParser(description="per-channel plots for BPW34 dual-gain capture")
    ap.add_argument("csv", type=str, help="capture CSV from 04_bpw34_dual_capture.py")
    ap.add_argument("--jpg", type=str, default=None, help="output prefix for JPGs")
    ap.add_argument("--pdf", type=str, default=None, help="output prefix for PDFs")
    ap.add_argument("--svg", type=str, default=None, help="output prefix for SVGs")
    ap.add_argument("--open", action="store_true", help="open generated files")
    ap.add_argument("--dpi", type=int, default=150, help="raster DPI for JPG (default 150)")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"  ✗ CSV not found: {csv_path}")
        sys.exit(1)

    meta, header, cols = read_capture(csv_path)

    # Which formats to emit. If none requested, default to JPGs beside the CSV.
    out_flags = []
    if args.jpg: out_flags.append(("jpg", args.jpg))
    if args.pdf: out_flags.append(("pdf", args.pdf))
    if args.svg: out_flags.append(("svg", args.svg))
    if not out_flags:
        default_prefix = str(csv_path.with_suffix(""))
        out_flags.append(("jpg", default_prefix))
        print(f"  (no output flag given → defaulting to JPGs at {default_prefix}_<channel>.jpg)")

    n_rows = len(cols.get("t_rel_s", []))
    print(f"  loaded {n_rows} rows from {csv_path}")
    if n_rows == 0:
        print("  ! no data rows — nothing to plot.")
        sys.exit(1)

    saved = make_figures(csv_path, meta, cols, out_flags, args.dpi)
    print(f"  {len(saved)} figure file(s) written.")
    if args.open and saved:
        open_files(saved)


if __name__ == "__main__":
    main()
