#!/usr/bin/env python3
"""
analysis/analyse_clouds.py
───────────────────────────
Post-eclipse cloud cover analysis from sky snapshots.

For each JPEG in ~/data/sky/:
  - Converts to HSV colour space
  - Blue pixels  (hue 90-130°, saturation >30%) = clear sky
  - White/grey pixels (saturation <25%, value >60%)  = cloud
  - Dark pixels (value <20%) = obstruction / night
  - Computes cloud fraction = cloud / (cloud + clear)

Works with your IR camera too:
  - IR images are greyscale-ish (no hue), so the script
    falls back to a luminance-based method for IR images
  - Clouds appear bright, clear IR sky appears dark

Outputs:
  - cloud_cover.csv   — timestamp, cloud%, clear%, dark%
  - cloud_cover.png   — timeline plot (if matplotlib installed)

Usage:
  python3 analysis/analyse_clouds.py
  python3 analysis/analyse_clouds.py --data /home/eclipse/data/sky/
  python3 analysis/analyse_clouds.py --plot
  python3 analysis/analyse_clouds.py --contact2 "2026-08-12T18:23:45Z"
"""

import sys, json, csv, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.hw import load_config

try:
    import numpy as np
    _NP = True
except ImportError:
    _NP = False
    print("numpy not installed — pip3 install numpy --break-system-packages")

try:
    from PIL import Image
    _PIL = True
except ImportError:
    _PIL = False
    print("Pillow not installed — pip3 install Pillow --break-system-packages")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    _MPL = True
except ImportError:
    _MPL = False

def analyse_frame_visible(img_array):
    """
    Analyse a visible-light RGB image.
    Returns (cloud_pct, clear_pct, dark_pct).
    """
    # Convert RGB to HSV
    r = img_array[:,:,0].astype(float) / 255.0
    g = img_array[:,:,1].astype(float) / 255.0
    b = img_array[:,:,2].astype(float) / 255.0

    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    diff = cmax - cmin

    # Value (brightness)
    v = cmax

    # Saturation
    s = np.where(cmax > 0.01, diff / cmax, 0)

    # Hue (0-360)
    h = np.zeros_like(r)
    mask_r = (cmax == r) & (diff > 0)
    mask_g = (cmax == g) & (diff > 0)
    mask_b = (cmax == b) & (diff > 0)
    h[mask_r] = 60 * (((g[mask_r] - b[mask_r]) / diff[mask_r]) % 6)
    h[mask_g] = 60 * ((b[mask_g] - r[mask_g]) / diff[mask_g] + 2)
    h[mask_b] = 60 * ((r[mask_b] - g[mask_b]) / diff[mask_b] + 4)

    total = r.size

    # Dark pixels (night, shadow, obstruction)
    dark  = np.sum(v < 0.15) / total * 100

    # Sky pixels: blue hue, some saturation
    clear = np.sum((h >= 85) & (h <= 135) & (s > 0.20) & (v > 0.2)) / total * 100

    # Cloud pixels: low saturation, bright
    cloud = np.sum((s < 0.25) & (v > 0.55)) / total * 100

    return round(cloud, 1), round(clear, 1), round(dark, 1)

def analyse_frame_ir(img_array):
    """
    Analyse an IR camera image (typically greyscale-ish).
    IR: clouds = bright (high reflectance), clear sky = dark.
    Returns (cloud_pct, clear_pct, dark_pct).
    """
    # Convert to greyscale if needed
    if img_array.ndim == 3:
        grey = np.mean(img_array, axis=2) / 255.0
    else:
        grey = img_array / 255.0

    total = grey.size

    # In IR: bright = cloud, dark = clear sky
    cloud = np.sum(grey > 0.65) / total * 100
    clear = np.sum(grey < 0.35) / total * 100
    dark  = np.sum(grey < 0.05) / total * 100

    return round(cloud, 1), round(clear, 1), round(dark, 1)

def is_ir_image(img_array):
    """
    Heuristic: if R, G, B channels are very similar the image
    is likely IR (no colour information).
    """
    if img_array.ndim < 3 or img_array.shape[2] < 3:
        return True
    r = img_array[:,:,0].astype(float)
    g = img_array[:,:,1].astype(float)
    b = img_array[:,:,2].astype(float)
    rg_diff = float(np.mean(np.abs(r - g)))
    rb_diff = float(np.mean(np.abs(r - b)))
    return rg_diff < 8 and rb_diff < 8   # nearly identical channels = IR

def main():
    parser = argparse.ArgumentParser(description="Cloud cover analysis")
    parser.add_argument("--data",      default=None,
                        help="Path to sky/ folder (default: ~/data/sky/)")
    parser.add_argument("--plot",      action="store_true",
                        help="Generate cloud cover timeline plot")
    parser.add_argument("--contact2",  default=None,
                        help="2nd contact UTC for timeline annotation")
    parser.add_argument("--contact3",  default=None,
                        help="3rd contact UTC for timeline annotation")
    args = parser.parse_args()

    cfg = load_config()

    if args.data:
        sky_dir = Path(args.data)
    else:
        sky_dir = Path(cfg["recording"]["output_dir"]) / "sky"

    if not sky_dir.exists():
        print(f"  Sky snapshot folder not found: {sky_dir}")
        print("  Run sky_snapshots.py first to collect images.")
        return

    print("\n╔══════════════════════════════════════════════════╗")
    print("║   Cloud Cover Analysis                         ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"\n  Source: {sky_dir}\n")

    if not _NP or not _PIL:
        print("  Missing dependencies:")
        if not _NP:
            print("    pip3 install numpy --break-system-packages")
        if not _PIL:
            print("    pip3 install Pillow --break-system-packages")
        return

    # Load index
    index_path = sky_dir / "index.json"
    if index_path.exists():
        with open(index_path) as f:
            index = json.load(f)
        file_ts = {e["file"]: e["ts_ns"] for e in index}
    else:
        file_ts = {}

    # Find all JPEG frames
    frames = sorted(sky_dir.glob("sky_*.jpg"))
    if not frames:
        print("  No sky_*.jpg files found.")
        return

    print(f"  Found {len(frames)} frames\n")
    print(f"  {'Frame':<30} {'Cloud%':>7} {'Clear%':>7} {'Dark%':>7}  Type")

    results = []
    for frame in frames:
        try:
            img   = Image.open(frame).convert("RGB")
            arr   = np.array(img)
            ir    = is_ir_image(arr)

            if ir:
                cloud, clear, dark = analyse_frame_ir(arr)
                kind = "IR"
            else:
                cloud, clear, dark = analyse_frame_visible(arr)
                kind = "visible"

            # Get timestamp
            ts_ns = file_ts.get(frame.name)
            if ts_ns:
                utc = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
            else:
                # Parse from filename: sky_YYYYMMDD_HHMMSS.jpg
                try:
                    ts_str = frame.stem.replace("sky_", "")
                    utc = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").replace(
                          tzinfo=timezone.utc)
                except:
                    utc = None

            results.append({
                "file":    frame.name,
                "utc":     utc.isoformat() if utc else "",
                "ts_ns":   ts_ns or 0,
                "cloud":   cloud,
                "clear":   clear,
                "dark":    dark,
                "type":    kind,
            })
            print(f"  {frame.name:<30} {cloud:>7.1f} {clear:>7.1f} "
                  f"{dark:>7.1f}  {kind}")

        except Exception as e:
            print(f"  {frame.name:<30} ERROR: {e}")

    if not results:
        print("  No frames analysed.")
        return

    # Summary stats
    clouds = [r["cloud"] for r in results]
    print(f"\n  {'─'*58}")
    print(f"  Frames analysed:     {len(results)}")
    print(f"  Mean cloud cover:    {sum(clouds)/len(clouds):.1f}%")
    print(f"  Max cloud cover:     {max(clouds):.1f}%")
    print(f"  Min cloud cover:     {min(clouds):.1f}%")

    # Eclipse window stats if contact times given
    if args.contact2:
        t2 = datetime.fromisoformat(args.contact2.replace("Z","+00:00"))
        t3 = datetime.fromisoformat(args.contact3.replace(
             "Z","+00:00")) if args.contact3 else t2 + timedelta(minutes=4)
        window_start = t2 - timedelta(minutes=5)
        window_end   = t3 + timedelta(minutes=5)
        window_results = [r for r in results if r["utc"] and
                          window_start <= datetime.fromisoformat(
                          r["utc"]) <= window_end]
        if window_results:
            wc = [r["cloud"] for r in window_results]
            print(f"\n  Eclipse window (±5min of totality):")
            print(f"    Frames:          {len(window_results)}")
            print(f"    Mean cloud:      {sum(wc)/len(wc):.1f}%")
            print(f"    {'CLEAR' if sum(wc)/len(wc) < 20 else 'PARTLY CLOUDY' if sum(wc)/len(wc) < 60 else 'OVERCAST'}")

    # Save CSV
    out_csv = sky_dir / "cloud_cover.csv"
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["file","utc","ts_ns",
                                           "cloud","clear","dark","type"])
        w.writeheader()
        w.writerows(results)
    print(f"\n  CSV saved: {out_csv}")

    # Plot
    if args.plot and _MPL and len(results) > 1:
        times  = [datetime.fromisoformat(r["utc"]) for r in results if r["utc"]]
        clouds = [r["cloud"] for r in results if r["utc"]]
        clears = [r["clear"] for r in results if r["utc"]]

        fig, ax = plt.subplots(figsize=(12, 4))
        ax.fill_between(times, clouds, alpha=0.4, color="#6B9FD4", label="Cloud %")
        ax.fill_between(times, clears, alpha=0.3, color="#F5C842", label="Clear sky %")
        ax.plot(times, clouds, color="#2E6DA4", linewidth=1.5)

        # Mark totality if given
        if args.contact2:
            ax.axvline(t2, color="black", linewidth=2, linestyle="--",
                       label="2nd contact")
            ax.axvline(t3, color="black", linewidth=1.5, linestyle=":",
                       label="3rd contact")
            ax.axvspan(t2, t3, alpha=0.08, color="black", label="Totality")

        ax.set_xlabel("UTC time")
        ax.set_ylabel("Coverage (%)")
        ax.set_title("Cloud cover from sky snapshots")
        ax.set_ylim(0, 105)
        ax.legend(loc="upper right", fontsize=8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        plt.xticks(rotation=30)
        plt.tight_layout()

        plot_path = sky_dir / "cloud_cover.png"
        plt.savefig(plot_path, dpi=150)
        print(f"  Plot saved: {plot_path}")
        plt.close()
    elif args.plot and not _MPL:
        print("  (install matplotlib for plots: "
              "pip3 install matplotlib --break-system-packages)")

    print()

if __name__ == "__main__":
    main()
