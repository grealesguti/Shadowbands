#!/usr/bin/env python3
"""
tests/07_plot.py
─────────────────
Plot a Shadowbands capture CSV **in the terminal** (works over plain SSH,
no X forwarding needed), with optional vector (PDF/SVG) or JPG export for
later inspection, plus a compact Parquet re-save of the data.

Reads the CSV written by 06_capture.py (t_rel_s, t_utc, ch0..chN).
Photosensor channels are numbered from 0 (ch0, ch1, ch2, ch3 ...).

Usage:
  python3 tests/07_plot.py data/totality_run1.csv
  python3 tests/07_plot.py data/run.csv --last 30            # last 30 s only
  python3 tests/07_plot.py data/run.csv --detrend            # show AC ripple
  python3 tests/07_plot.py data/run.csv --chans 0 2          # subset
  python3 tests/07_plot.py data/run.csv --pdf out.pdf        # vector export
  python3 tests/07_plot.py data/run.csv --svg out.svg        # vector export
  python3 tests/07_plot.py data/run.csv --jpg out.jpg        # raster, max dpi
  python3 tests/07_plot.py data/run.csv --parquet out.parquet# compact re-save
  python3 tests/07_plot.py data/run.csv --open               # open figure after
  python3 tests/07_plot.py data/run.csv --follow             # live tail (grows)
  python3 tests/07_plot.py data/run.csv --width 120 --height 24
  python3 tests/07_plot.py --benchmark                       # speed stress test

--detrend subtracts a 0.5 s moving average per channel: the slow light level
disappears and the few-percent shadowband-style ripple becomes visible.

Vector export (--pdf/--svg) and --jpg need matplotlib
(sudo apt install -y python3-matplotlib). Parquet needs pyarrow
(pip3 install pyarrow --break-system-packages). The terminal plot needs
nothing beyond numpy.
"""
import sys, os, csv, time, argparse, math, subprocess, platform
from pathlib import Path

try:
    import numpy as np
except ImportError:
    print("Needs numpy:  sudo pip3 install numpy --break-system-packages")
    sys.exit(1)

# 0-indexed photosensor channels: ch0..ch7 supported for coloring/marks
COLORS = {  # ANSI per channel index
    "0": "\033[92m",  # green
    "1": "\033[93m",  # yellow
    "2": "\033[96m",  # cyan
    "3": "\033[95m",  # magenta
    "4": "\033[91m",  # red
    "5": "\033[94m",  # blue
    "6": "\033[97m",  # white
    "7": "\033[90m",  # grey
}
RESET = "\033[0m"
MARKS = {"0": "•", "1": "×", "2": "▪", "3": "+", "4": "○", "5": "*", "6": "◇", "7": "△"}


def _chan_name(header_field):
    """Normalize a header like 'chA', 'ch0', 'A', '0' to a 0-based index string.

    Legacy A/B/C/D headers map to 0/1/2/3 so old CSVs still load, but all
    output uses 0-indexed names.
    """
    s = header_field
    if s.lower().startswith("ch"):
        s = s[2:]
    if s.isalpha() and len(s) == 1:
        return str(ord(s.upper()) - ord("A"))
    return s


def load_csv(path, last_s=None):
    t, cols, names = [], None, None
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        names = [_chan_name(h) for h in header[2:]]   # chA/ch0 -> "0" ...
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
    legend = "   ".join(
        f"{COLORS.get(n,'')}{MARKS.get(n,'·')} ch{n}{RESET}" for n in series)
    print(f"  {legend}")
    for row in range(height):
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
    print(f"  {'ch':<5}{'mean':>8}{'std':>8}{'min':>7}{'max':>7}{'p-p':>7}")
    for n, v in data.items():
        print(f"  ch{n:<3}{v.mean():>8.1f}{v.std():>8.2f}{v.min():>7d}"
              f"{v.max():>7d}{int(v.max()-v.min()):>7d}")
    return fs


def save_vector(t, data, path, detrended, fs, fmt, dpi=300, do_open=False):
    """Save a figure. Vector (pdf/svg) keeps full precision; jpg is high-dpi."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  ! figure skipped: matplotlib missing "
              "(sudo apt install -y python3-matplotlib)")
        return None
    fig, ax = plt.subplots(figsize=(12, 5), dpi=dpi)
    for n, v in data.items():
        ax.plot(t, v, lw=0.7, label=f"ch{n}")
    ax.set_xlabel("t_rel [s]")
    ax.set_ylabel("counts (detrended)" if detrended else "counts")
    ax.legend(ncol=4, fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    if fmt in ("pdf", "svg"):
        # vector: no rasterization, full numeric precision preserved
        fig.savefig(path, format=fmt)
    else:
        # jpg: highest reasonable quality
        fig.savefig(path, format="jpg", dpi=dpi, pil_kwargs={"quality": 95})
    plt.close(fig)
    print(f"  {fmt.upper():5}: {path}")
    if do_open:
        _open_file(path)
    return path


def _open_file(path):
    """Open a file with the platform's default viewer, if possible."""
    try:
        sysname = platform.system()
        if sysname == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif sysname == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
                subprocess.Popen(["xdg-open", str(path)],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            else:
                print(f"  (no display; open manually: {path})")
    except Exception as e:
        print(f"  ! could not open {path}: {e}")


def save_parquet(t, data, path):
    """Re-save the capture in a compact, low-footprint, exactly-recoverable
    columnar format. Counts stay integer; time stays float64 (full precision).
    Compression keeps disk/memory use low; the data reads back losslessly."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        print("  ! Parquet skipped: pyarrow missing "
              "(pip3 install pyarrow --break-system-packages)")
        return None
    arrays = {"t_rel_s": pa.array(t.astype(np.float64))}
    for n, v in data.items():
        # keep the smallest int type that holds the data, losslessly
        vmax = int(v.max()) if len(v) else 0
        if vmax < 2**16:
            arr = v.astype(np.uint16)
        elif vmax < 2**32:
            arr = v.astype(np.uint32)
        else:
            arr = v.astype(np.int64)
        arrays[f"ch{n}"] = pa.array(arr)
    table = pa.table(arrays)
    pq.write_table(table, path, compression="zstd", use_dictionary=False)
    sz = Path(path).stat().st_size
    print(f"  PARQ : {path}  ({sz/1024:.1f} KiB, zstd)")
    return path


def benchmark():
    """Stress the acquisition/serialization path to find the max sustainable
    sample rate on this machine. Measures pure in-memory generation, CSV write,
    and Parquet write throughput for a 4-channel photosensor array (ch0..ch3).
    This bounds how fast the capture side can be persisted without dropping."""
    print("\n  === speed stress test (4 channels: ch0..ch3) ===")
    n_chans = 4
    sizes = [10_000, 100_000, 1_000_000, 5_000_000]
    tmp = Path("_bench_tmp")
    tmp.mkdir(exist_ok=True)
    try:
        for n in sizes:
            # in-memory generation throughput (uint16 counts + float64 time)
            t0 = time.perf_counter()
            t = np.arange(n, dtype=np.float64) / 1000.0
            data = {str(c): (np.random.rand(n) * 60000).astype(np.uint16)
                    for c in range(n_chans)}
            gen = time.perf_counter() - t0

            # CSV write throughput (worst case, human-readable)
            csv_path = tmp / "b.csv"
            t0 = time.perf_counter()
            with open(csv_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["t_rel_s", "t_utc"] + [f"ch{c}" for c in range(n_chans)])
                cols = [data[str(c)] for c in range(n_chans)]
                for i in range(n):
                    w.writerow([f"{t[i]:.6f}", ""] + [int(cols[c][i]) for c in range(n_chans)])
            csv_w = time.perf_counter() - t0
            csv_sz = csv_path.stat().st_size

            # Parquet write throughput (compact target format)
            parq_sz = pw = float("nan")
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
                pq_path = tmp / "b.parquet"
                t0 = time.perf_counter()
                arrays = {"t_rel_s": pa.array(t)}
                for c in range(n_chans):
                    arrays[f"ch{c}"] = pa.array(data[str(c)])
                pq.write_table(pa.table(arrays), pq_path, compression="zstd")
                pw = time.perf_counter() - t0
                parq_sz = pq_path.stat().st_size
            except ImportError:
                pass

            print(f"\n  N = {n:>9,}  ({n_chans} ch)")
            print(f"    generate : {gen*1e3:8.1f} ms  -> {n/gen/1e6:7.2f} M samp/s")
            print(f"    CSV write: {csv_w*1e3:8.1f} ms  -> {n/csv_w/1e6:7.2f} M rows/s"
                  f"   ({csv_sz/1e6:6.1f} MB)")
            if not math.isnan(pw):
                print(f"    PARQ writ: {pw*1e3:8.1f} ms  -> {n/pw/1e6:7.2f} M rows/s"
                      f"   ({parq_sz/1e6:6.1f} MB, "
                      f"{csv_sz/parq_sz:4.1f}x smaller than CSV)")

        # peak sustainable ingest estimate from the largest run
        print("\n  Interpretation: the slowest write stage bounds sustainable")
        print("  acquisition rate. Parquet is the recommended persistence")
        print("  format (compact, low memory, lossless, fast to re-read).")
    finally:
        for p in tmp.glob("*"):
            p.unlink()
        tmp.rmdir()


def main():
    ap = argparse.ArgumentParser(description="terminal plot for capture CSVs")
    ap.add_argument("csvfile", type=str, nargs="?")
    ap.add_argument("--last", type=float, default=None,
                    help="only plot the last N seconds")
    ap.add_argument("--chans", type=str, nargs="+", default=None,
                    help="subset of channels, e.g. --chans 0 2")
    ap.add_argument("--detrend", action="store_true",
                    help="subtract 0.5 s moving average (show AC ripple)")
    ap.add_argument("--width", type=int, default=100)
    ap.add_argument("--height", type=int, default=20)
    ap.add_argument("--pdf", type=str, default=None,
                    help="save a vector PDF here (full precision)")
    ap.add_argument("--svg", type=str, default=None,
                    help="save a vector SVG here (full precision)")
    ap.add_argument("--jpg", type=str, default=None,
                    help="save a high-dpi JPG here")
    ap.add_argument("--dpi", type=int, default=300,
                    help="raster dpi for --jpg (default 300)")
    ap.add_argument("--parquet", type=str, default=None,
                    help="re-save data as compact Parquet (zstd) here")
    ap.add_argument("--open", action="store_true",
                    help="open the saved figure after the run")
    ap.add_argument("--follow", action="store_true",
                    help="live view: re-read and re-plot every 2 s")
    ap.add_argument("--benchmark", action="store_true",
                    help="run the acquisition speed stress test and exit")
    args = ap.parse_args()

    if args.benchmark:
        benchmark()
        return

    if not args.csvfile:
        ap.error("csvfile is required (or use --benchmark)")

    path = Path(args.csvfile)
    if not path.exists():
        print(f"  no such file: {path}"); sys.exit(1)

    def render():
        t, data = load_csv(path, last_s=args.last)
        if len(t) < 4:
            print("  (not enough samples yet)"); return
        if args.chans:
            want = [_chan_name(c) for c in args.chans]
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
        if args.pdf:
            save_vector(t, plot_data, args.pdf, args.detrend, fs, "pdf",
                        do_open=args.open)
        if args.svg:
            save_vector(t, plot_data, args.svg, args.detrend, fs, "svg",
                        do_open=args.open)
        if args.jpg:
            save_vector(t, plot_data, args.jpg, args.detrend, fs, "jpg",
                        dpi=args.dpi, do_open=args.open)
        # store raw (non-detrended) data losslessly for later analysis
        if args.parquet:
            save_parquet(t, data, args.parquet)

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
