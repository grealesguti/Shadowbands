#!/usr/bin/env python3
"""
tests/04_bpw34_dual_plotting.py
───────────────────────────────
Plotter for 04_bpw34_dual_capture.py. Reads the dual-gain BPW34 capture CSV
(with its "# key: value" metadata header) and renders a multi-panel figure:

  1. Raw ADC counts, both channels (LOW / HIGH) vs time — see saturation.
  2. Back-calculated photocurrent (nA), log scale, with the stitched BEST track
     that switches to the low-gain channel wherever the high-gain one rails.
  3. Power spectrum of the BEST track in the shadow-band region (0.5–20 Hz),
     with the ~4.5 Hz shadow-band reference marked.

Mirrors the AS7343 plotter's CLI so it plugs straight into the capture script's
--pdf/--svg/--jpg/--open handoff. If no output path is given it writes a PDF
next to the CSV.

Usage:
  python3 tests/04_bpw34_dual_plotting.py data/bpw34_run.csv --pdf out.pdf
  python3 tests/04_bpw34_dual_plotting.py data/bpw34_run.csv --open
"""
import sys, csv, argparse, subprocess
from pathlib import Path

try:
    import numpy as np
except ImportError:
    print("This plotter needs numpy: pip3 install numpy --break-system-packages")
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("Agg")                    # headless-safe; no X needed on the Pi
    import matplotlib.pyplot as plt
except ImportError:
    print("This plotter needs matplotlib: pip3 install matplotlib --break-system-packages")
    sys.exit(1)


SAT_COUNTS_DEFAULT = 4000


def load_csv(path):
    """Read the capture CSV. Returns (meta:dict, colnames:list, data:dict[str->np.array])."""
    meta = {}
    rows = []
    header = None
    with open(path, newline="") as f:
        for line in f:
            if line.startswith("#"):
                # "# key: value"
                body = line[1:].strip()
                if ":" in body:
                    k, v = body.split(":", 1)
                    meta[k.strip()] = v.strip()
                continue
            # first non-comment line is the CSV header
            reader = csv.reader([line])
            fields = next(reader)
            if header is None:
                header = fields
                continue
            rows.append(fields)
    if header is None:
        raise ValueError("no CSV header found (all comment lines?)")

    data = {name: [] for name in header}
    for r in rows:
        for name, val in zip(header, r):
            data[name].append(val)

    # numeric columns → float arrays; leave t_utc as strings
    out = {}
    for name, col in data.items():
        if name == "t_utc":
            out[name] = np.array(col, dtype=object)
        else:
            out[name] = np.array([float(x) if x not in ("", None) else np.nan
                                  for x in col], dtype=float)
    return meta, header, out


def power_spectrum(x, fs, lo=0.5, hi=20.0):
    """Hann-windowed magnitude spectrum of detrended x within [lo,hi] Hz."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 16:
        return np.array([]), np.array([])
    x = x - x.mean()
    w = np.hanning(len(x))
    X = np.abs(np.fft.rfft(x * w))
    f = np.fft.rfftfreq(len(x), d=1.0 / fs)
    band = (f >= lo) & (f <= hi)
    return f[band], X[band]


def make_figure(meta, d, sat_counts):
    t = d["t_rel_s"]
    fs = None
    if len(t) > 2:
        dt = np.diff(t)
        dt = dt[dt > 0]
        if len(dt):
            fs = 1.0 / np.median(dt)

    fig, ax = plt.subplots(3, 1, figsize=(9, 10.5))
    sensor = meta.get("sensor", "BPW34 dual-gain")
    rf_lo = meta.get("rf_low_ohm", "?"); rf_hi = meta.get("rf_high_ohm", "?")
    fig.suptitle(f"{sensor}\nRf_low={rf_lo}\u03a9  Rf_high={rf_hi}\u03a9  "
                 f"(~{fs:.0f} sps/ch)" if fs else sensor,
                 fontsize=11, y=0.995)

    # ---- panel 1: raw counts, both channels ----
    a = ax[0]
    a.plot(t, d["LOW"], lw=0.8, label="LOW gain (bright ch)", color="#1f77b4")
    a.plot(t, d["HIGH"], lw=0.8, label="HIGH gain (dim ch)", color="#d62728")
    a.axhline(sat_counts, ls="--", lw=0.8, color="gray",
              label=f"sat \u2265 {sat_counts}")
    a.axhline(4095, ls=":", lw=0.8, color="k")
    a.set_ylabel("ADC counts (0\u20134095)")
    a.set_title("Raw ADC \u2014 watch which channel is on-scale")
    a.set_ylim(-50, 4200)
    a.legend(fontsize=8, loc="upper right")
    a.grid(alpha=0.3)

    # ---- panel 2: photocurrent, log, with BEST stitch ----
    b = ax[1]
    # guard against non-positive for log axis
    def pos(y):
        y = np.array(y, dtype=float)
        y[y <= 0] = np.nan
        return y
    if "LOW_nA" in d:
        b.plot(t, pos(d["LOW_nA"]), lw=0.7, alpha=0.6,
               label="I_pd via LOW", color="#1f77b4")
    if "HIGH_nA" in d:
        b.plot(t, pos(d["HIGH_nA"]), lw=0.7, alpha=0.6,
               label="I_pd via HIGH", color="#d62728")
    if "BEST_nA" in d:
        b.plot(t, pos(d["BEST_nA"]), lw=1.3, color="k",
               label="BEST (stitched)")
    b.set_yscale("log")
    b.set_ylabel("photocurrent (nA, log)")
    b.set_title("Back-calculated I_pd \u2014 BEST switches channels at saturation")
    b.legend(fontsize=8, loc="upper right")
    b.grid(alpha=0.3, which="both")

    # ---- panel 3: shadow-band spectrum of BEST ----
    c = ax[2]
    track = d.get("BEST_nA")
    if track is None:
        track = d.get("HIGH_nA", d.get("LOW"))
    if fs and fs > 2:
        f, X = power_spectrum(track, fs, 0.5, min(20.0, fs / 2 - 0.1))
        if len(f):
            c.plot(f, X, lw=0.9, color="#2ca02c")
            c.axvline(4.5, ls="--", lw=1.0, color="orange",
                      label="shadow bands \u2248 4.5 Hz")
            k = np.argmax(X)
            c.plot(f[k], X[k], "o", color="red", ms=5)
            c.annotate(f"peak {f[k]:.2f} Hz", (f[k], X[k]),
                       textcoords="offset points", xytext=(6, 4), fontsize=8)
            c.legend(fontsize=8)
        else:
            c.text(0.5, 0.5, "not enough samples for a spectrum",
                   ha="center", va="center", transform=c.transAxes)
    else:
        c.text(0.5, 0.5, "sample rate too low / unknown for a spectrum",
               ha="center", va="center", transform=c.transAxes)
    c.set_xlabel("frequency (Hz)")
    c.set_ylabel("|FFT| (a.u.)")
    c.set_title("Shadow-band region (0.5\u201320 Hz) of the BEST track")
    c.grid(alpha=0.3)

    ax[0].set_xlabel("")
    ax[1].set_xlabel("time (s)")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


def main():
    ap = argparse.ArgumentParser(description="plot dual-gain BPW34 capture CSV")
    ap.add_argument("csv", help="capture CSV from 04_bpw34_dual_capture.py")
    ap.add_argument("--pdf", type=str, default=None)
    ap.add_argument("--svg", type=str, default=None)
    ap.add_argument("--jpg", type=str, default=None)
    ap.add_argument("--open", action="store_true",
                    help="open the produced file with the OS viewer")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"  \u2717 CSV not found: {csv_path}")
        sys.exit(1)

    meta, header, d = load_csv(csv_path)
    if "t_rel_s" not in d or len(d["t_rel_s"]) < 2:
        print("  \u2717 CSV has no usable time series.")
        sys.exit(1)

    sat = int(float(meta.get("sat_counts", SAT_COUNTS_DEFAULT)))
    fig = make_figure(meta, d, sat)

    # default to a PDF next to the CSV if no explicit target given
    targets = []
    if args.pdf: targets.append(("pdf", Path(args.pdf)))
    if args.svg: targets.append(("svg", Path(args.svg)))
    if args.jpg: targets.append(("jpg", Path(args.jpg)))
    if not targets:
        targets.append(("pdf", csv_path.with_suffix(".pdf")))

    written = []
    for kind, path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "jpg":
            fig.savefig(path, dpi=150, format="jpg")
        else:
            fig.savefig(path, format=kind)
        print(f"  \u2713 wrote {kind.upper()}: {path}")
        written.append(path)

    plt.close(fig)

    if args.open and written:
        target = written[0]
        for opener in ("xdg-open", "open"):
            try:
                subprocess.run([opener, str(target)], check=False)
                break
            except FileNotFoundError:
                continue


if __name__ == "__main__":
    main()
