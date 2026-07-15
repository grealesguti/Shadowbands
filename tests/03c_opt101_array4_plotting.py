#!/usr/bin/env python3
"""
tests/07_plot.py
─────────────────
Plot a Shadowbands capture CSV **in the terminal** (works over plain SSH,
no X forwarding needed), with optional PNG export for later inspection.

Reads the CSV written by 06_capture.py (t_rel_s, t_utc, chA..chD).

Usage:
  python3 tests/07_plot.py data/totality_run1.csv
  python3 tests/07_plot.py data/run.csv --last 30          # last 30 s only
  python3 tests/07_plot.py data/run.csv --detrend          # show AC ripple
  python3 tests/07_plot.py data/run.csv --chans A C        # subset
  python3 tests/07_plot.py data/run.csv --png out.png      # also save PNG
  python3 tests/07_plot.py data/run.csv --follow           # live tail (grows)
  python3 tests/07_plot.py data/run.csv --width 120 --height 24

--detrend subtracts a 0.5 s moving average per channel: the slow light level
disappears and the few-percent shadowband-style ripple becomes visible.

PNG export needs matplotlib (sudo apt install -y python3-matplotlib); the
terminal plot needs nothing beyond numpy.
"""
import sys, os, csv, time, argparse, math
from pathlib import Path

try:
    import numpy as np
except ImportError:
    print("Needs numpy:  sudo pip3 install numpy --break-system-packages")
    sys.exit(1)

COLORS = {  # ANSI per channel
    "A": "\033[92m",  # green
    "B": "\033[93m",  # yellow
    "C": "\033[96m",  # cyan
    "D": "\033[95m",  # magenta
}
RESET = "\033[0m"
MARKS = {"A": "•", "B": "×", "C": "▪", "D": "+"}


def load_csv(path, last_s=None):
    t, cols, names = [], None, None
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        names = [h[2:] for h in header[2:]]           # chA -> A
        cols = [[] for _ in names]
        for row in r:
            if not row:
                continue
            t.append(float(row[0]))
            for j in range(len(names)):
                cols[j].append(int(row[2 + j]))
    t = np.array(t)
    data = {n: np.array(c) for n, c in zip(names, cols)}
    if last_s is not None and len(t) and t[-1] - t[0] > last_s:
        keep = t >= (t[-1] - last_s)
        t = t[keep]
        data = {n: v[keep] for n, v in data.items()}
    return t, data


def moving_avg(x, w):
    pad = w // 2
    xp = np.pad(x, pad, mode="edge")
    k = np.ones(w) / w
    return np.convolve(xp, k, mode="valid")[:len(x)]


def detrend(x, fs, win_s=0.5):
    w = max(3, int(fs * win_s))
    if w >= len(x):
        return x - x.mean()
    return x - moving_avg(x.astype(float), w)


def downsample(t, y, n_out):
    """Min/max-preserving decimation so spikes aren't lost on a small screen."""
    n = len(t)
    if n <= n_out:
        return t, y
    idx = np.linspace(0, n, n_out + 1).astype(int)
    tt, yy = [], []
    for a, b in zip(idx[:-1], idx[1:]):
        seg = y[a:b]
        if not len(seg):
            continue
        lo, hi = int(np.argmin(seg)), int(np.argmax(seg))
        first, second = (lo, hi) if lo < hi else (hi, lo)
        tt.extend([t[a + first], t[a + second]])
        yy.extend([seg[first], seg[second]])
    return np.array(tt), np.array(yy)


def term_plot(t, series, width=100, height=20, title="", ylabel="counts"):
    """Render overlaid channel traces as a Unicode grid with ANSI colors."""
    ymin = min(float(np.min(v)) for v in series.values())
    ymax = max(float(np.max(v)) for v in series.values())
    if ymax - ymin < 1e-9:
        ymax = ymin + 1.0
    t0, t1 = float(t[0]), float(t[-1]) if len(t) > 1 else float(t[0]) + 1.0

    grid = [[" "] * width for _ in range(height)]
    colr = [[None] * width for _ in range(height)]

    for name, y in series.items():
        td, yd = downsample(t, y, width * 2)
        for ti, yi in zip(td, yd):
            cx = int((ti - t0) / (t1 - t0 + 1e-12) * (width - 1))
            cy = int((yi - ymin) / (ymax - ymin) * (height - 1))
            cy = height - 1 - cy
            cx = min(max(cx, 0), width - 1)
            cy = min(max(cy, 0), height - 1)
            grid[cy][cx] = MARKS.get(name, "·")
            colr[cy][cx] = COLORS.get(name, "")

    print()
    if title:
        print(f"  {title}")
    legend = "   ".join(f"{COLORS[n]}{MARKS[n]} {n}{RESET}" for n in series)
    print(f"  {legend}")
    for row in range(height):
        # y-axis label on a few rows
        if row == 0:
            lab = f"{ymax:8.0f} ┤"
        elif row == height - 1:
            lab = f"{ymin:8.0f} ┤"
        elif row == height // 2:
            lab = f"{(ymin+ymax)/2:8.0f} ┤"
        else:
            lab = "         │"
        line = "".join(
            (colr[row][cx] + grid[row][cx] + RESET) if colr[row][cx] else grid[row][cx]
            for cx in range(width))
        print(f"  {lab}{line}")
    axis = "         └" + "─" * width
    print(f"  {axis}")
    print(f"  {'':9} {t0:<10.1f}{'t_rel [s]':^{max(0,width-20)}}{t1:>10.1f}")
    print(f"  y: {ylabel}   span {ymax-ymin:.0f}")


def summarize(t, data):
    fs = (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 else float("nan")
    print(f"\n  {len(t)} samples   {t[-1]-t[0]:.1f} s   ~{fs:.0f} sps")
    print(f"  {'ch':<4}{'mean':>8}{'std':>8}{'min':>7}{'max':>7}{'p-p':>7}")
    for n, v in data.items():
        print(f"  {n:<4}{v.mean():>8.1f}{v.std():>8.2f}{v.min():>7d}"
              f"{v.max():>7d}{int(v.max()-v.min()):>7d}")
    return fs


def save_png(t, data, path, detrended, fs):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  ! PNG skipped: matplotlib missing "
              "(sudo apt install -y python3-matplotlib)")
        return
    fig, ax = plt.subplots(figsize=(12, 5), dpi=110)
    for n, v in data.items():
        ax.plot(t, v, lw=0.7, label=n)
    ax.set_xlabel("t_rel [s]")
    ax.set_ylabel("counts (detrended)" if detrended else "counts")
    ax.legend(ncol=4, fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path)
    print(f"  PNG  : {path}")


def main():
    ap = argparse.ArgumentParser(description="terminal plot for capture CSVs")
    ap.add_argument("csvfile", type=str)
    ap.add_argument("--last", type=float, default=None,
                    help="only plot the last N seconds")
    ap.add_argument("--chans", type=str, nargs="+", default=None,
                    help="subset of channels, e.g. --chans A C")
    ap.add_argument("--detrend", action="store_true",
                    help="subtract 0.5 s moving average (show AC ripple)")
    ap.add_argument("--width", type=int, default=100)
    ap.add_argument("--height", type=int, default=20)
    ap.add_argument("--png", type=str, default=None,
                    help="also save a matplotlib PNG here")
    ap.add_argument("--follow", action="store_true",
                    help="live view: re-read and re-plot every 2 s")
    args = ap.parse_args()

    path = Path(args.csvfile)
    if not path.exists():
        print(f"  no such file: {path}"); sys.exit(1)

    def render():
        t, data = load_csv(path, last_s=args.last)
        if len(t) < 4:
            print("  (not enough samples yet)"); return
        if args.chans:
            want = [c.upper() for c in args.chans]
            data = {n: v for n, v in data.items() if n in want}
            if not data:
                print(f"  no matching channels {want}"); sys.exit(1)
        fs = summarize(t, data)
        plot_data = data
        ylabel = "counts"
        if args.detrend:
            plot_data = {n: detrend(v, fs) for n, v in data.items()}
            ylabel = "counts (detrended, 0.5 s HP)"
        term_plot(t, plot_data, width=args.width, height=args.height,
                  title=str(path.name) +
                        (f"  [last {args.last:.0f}s]" if args.last else ""),
                  ylabel=ylabel)
        if args.png:
            save_png(t, plot_data, args.png, args.detrend, fs)

    if args.follow:
        try:
            while True:
                os.system("clear")
                render()
                print("\n  (following — Ctrl+C to stop)")
                time.sleep(2.0)
        except KeyboardInterrupt:
            print("\n  stopped.")
    else:
        render()


if __name__ == "__main__":
    main()
