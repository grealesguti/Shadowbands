#!/usr/bin/env python3
"""
04b_mpu6050_debug.py  --  MPU-6050 bring-up & gravity-direction test (I2C).

Digital IMU over I2C -- unlike the analog ADXL335 it costs ZERO MCP3208
channels, leaving all 8 ADC inputs for the photodiode array.

WIRING:
    MPU-6050 VCC -> 3.3 V        (chip is 3.3 V; most breakouts regulate)
    MPU-6050 GND -> GND
    MPU-6050 SDA -> Pi GPIO 2  (physical pin 3)
    MPU-6050 SCL -> Pi GPIO 3  (physical pin 5)
    MPU-6050 AD0 -> GND          (I2C address 0x68; tie high for 0x69)
    XDA / XCL / INT -> leave floating

SETUP:
    sudo raspi-config      -> Interface Options -> I2C -> Enable ; reboot
    sudo apt install -y i2c-tools python3-smbus
    i2cdetect -y 1         -> must show 68

WHAT THIS PROVIDES (and does NOT):
    PROVIDES: TILT (roll/pitch) from gravity -- an ABSOLUTE reference, since
              gravity always points down. This is the piece that resolves the
              roll ambiguity so ONE Sun fix fully determines frame azimuth.
    DOES NOT: heading / azimuth. The MPU-6050 has NO magnetometer. Azimuth
              comes from the Sun / shadow / GNSS methods.

PHYSICS CHECKED:
    At rest the accelerometer measures ONLY gravity: a 1 g vector pointing
    DOWN in the sensor's frame.
      MAGNITUDE  sqrt(ax^2+ay^2+az^2) must be ~1.00 g in ANY orientation.
      DIRECTION  of that vector gives roll/pitch = the board's tilt.
    Flat & level -> gravity lands entirely on Z (|Z| ~ 1 g, X ~ 0, Y ~ 0).

    The GYRO is used only to confirm the board is STILL during captures
    (a moving board adds linear acceleration and breaks the |g|=1 assumption)
    and, in the field, to flag that the frame was nudged.

Run:  sudo python3 tests/04b_mpu6050_debug.py [--recal]
"""

import os
import sys
import json
import time
import math
import argparse

try:
    import numpy as np
except ImportError:
    print("Needs numpy:  sudo pip3 install numpy --break-system-packages")
    sys.exit(1)
try:
    import smbus2 as smbus
except ImportError:
    try:
        import smbus
    except ImportError:
        print("Needs smbus:  sudo apt install -y python3-smbus  (or pip3 install smbus2)")
        sys.exit(1)

CAL_FILE = "mpu6050_cal.json"

# --- MPU-6050 registers ---
PWR_MGMT_1   = 0x6B
WHO_AM_I     = 0x75
SMPLRT_DIV   = 0x19
CONFIG       = 0x1A
GYRO_CONFIG  = 0x1B
ACCEL_CONFIG = 0x1C
ACCEL_XOUT_H = 0x3B

ACCEL_FS = {2: (0x00, 16384.0), 4: (0x08, 8192.0),
            8: (0x10, 4096.0), 16: (0x18, 2048.0)}
GYRO_LSB = 131.0   # +/-250 dps


class MPU6050:
    def __init__(self, bus_id=1, addr=0x68, fs_g=2):
        self.bus = smbus.SMBus(bus_id)
        self.addr = addr
        self.accel_lsb = ACCEL_FS[fs_g][1]
        # wake up (clear sleep bit), use gyro X as clock source (more stable)
        self.bus.write_byte_data(addr, PWR_MGMT_1, 0x01)
        time.sleep(0.1)
        # DLPF ~44 Hz: heavy low-pass is GOOD for static tilt (kills vibration)
        self.bus.write_byte_data(addr, CONFIG, 0x03)
        self.bus.write_byte_data(addr, SMPLRT_DIV, 0x09)      # 100 Hz
        self.bus.write_byte_data(addr, ACCEL_CONFIG, ACCEL_FS[fs_g][0])
        self.bus.write_byte_data(addr, GYRO_CONFIG, 0x00)     # +/-250 dps
        time.sleep(0.05)

    def who_am_i(self):
        return self.bus.read_byte_data(self.addr, WHO_AM_I)

    def _s16(self, hi, lo):
        v = (hi << 8) | lo
        return v - 65536 if v > 32767 else v

    def read_raw(self):
        """Burst-read accel(3), temp, gyro(3) -> (a[3] in g, gyro[3] in dps, temp C)."""
        d = self.bus.read_i2c_block_data(self.addr, ACCEL_XOUT_H, 14)
        ax = self._s16(d[0], d[1]) / self.accel_lsb
        ay = self._s16(d[2], d[3]) / self.accel_lsb
        az = self._s16(d[4], d[5]) / self.accel_lsb
        temp = self._s16(d[6], d[7]) / 340.0 + 36.53
        gx = self._s16(d[8], d[9]) / GYRO_LSB
        gy = self._s16(d[10], d[11]) / GYRO_LSB
        gz = self._s16(d[12], d[13]) / GYRO_LSB
        return np.array([ax, ay, az]), np.array([gx, gy, gz]), temp

    def close(self):
        try:
            self.bus.close()
        except Exception:
            pass


def ok(d, x=""):   print(f"  \u2713 [PASS]  {d:<44s} {x}")
def bad(d, x=""):  print(f"  \u2717 [FAIL]  {d:<44s} {x}")
def warn(d, x=""): print(f"  ! [WARN]  {d:<44s} {x}")


def average(imu, n=200, dt=0.005):
    """Average n samples -> (accel g[3], gyro dps[3], gyro_std[3], temp)."""
    A = np.zeros((n, 3)); G = np.zeros((n, 3)); T = 0.0
    for i in range(n):
        a, g, t = imu.read_raw()
        A[i] = a; G[i] = g; T += t
        time.sleep(dt)
    return A.mean(axis=0), G.mean(axis=0), G.std(axis=0), T / n


def is_still(gyro_mean, gyro_std, thresh=2.0):
    """Board static? gyro should read ~0 dps with small spread."""
    return (np.abs(gyro_mean).max() < thresh) and (gyro_std.max() < thresh)


def apply_cal(a, bias, scale):
    return (a - bias) * scale


def tilt_from_g(g):
    """Gravity vector -> (roll_deg, pitch_deg, tilt_from_vertical_deg, |g|)."""
    gx, gy, gz = float(g[0]), float(g[1]), float(g[2])
    norm = math.sqrt(gx*gx + gy*gy + gz*gz)
    roll = math.degrees(math.atan2(gy, gz))
    pitch = math.degrees(math.atan2(-gx, math.sqrt(gy*gy + gz*gz)))
    if norm > 1e-9:
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, abs(gz) / norm))))
    else:
        tilt = float("nan")
    return roll, pitch, tilt, norm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x68)
    ap.add_argument("--bus", type=int, default=1)
    ap.add_argument("--recal", action="store_true")
    args = ap.parse_args()

    print()
    print("\u2554" + "\u2550" * 54 + "\u2557")
    print("\u2551  04b \u2014 MPU-6050 bring-up & gravity-direction test    \u2551")
    print("\u255a" + "\u2550" * 54 + "\u255d")
    print(f"\n  I2C bus {args.bus}, addr 0x{args.addr:02X}   (uses NO MCP3208 channels)")
    print("  Gives TILT only \u2014 no magnetometer, so azimuth still comes from the Sun.\n")

    passes, total = 0, 6

    # ---------- STAGE 0: device present & genuine ----------
    try:
        imu = MPU6050(bus_id=args.bus, addr=args.addr)
    except Exception as e:
        bad("I2C device reachable", f"{e}")
        print("    \u2192 Enable I2C (raspi-config), then: i2cdetect -y 1  \u2192 expect 68")
        return
    ok("I2C device reachable", f"0x{args.addr:02X}")

    print("\n  STAGE 0 \u2014 IDENTITY (WHO_AM_I)")
    try:
        wai = imu.who_am_i()
    except Exception as e:
        bad("WHO_AM_I readable", str(e)); return
    print(f"    WHO_AM_I = 0x{wai:02X}   (genuine MPU-6050 returns 0x68)")
    if wai == 0x68:
        ok("Genuine MPU-6050 signature", "0x68")
        passes += 1
    elif wai in (0x70, 0x71, 0x73, 0x74, 0x75, 0x98):
        warn("Genuine MPU-6050 signature",
             f"0x{wai:02X} \u2014 likely a clone (MPU-6500/9250 die)")
        print("    \u2192 Clones usually still work for tilt. Continuing.")
        passes += 1
    else:
        bad("Genuine MPU-6050 signature", f"unexpected 0x{wai:02X}")
        print("    \u2192 Counterfeit/relabeled board, or a bus/address problem.")

    # ---------- STAGE 1: raw reading sane ----------
    print("\n  STAGE 1 \u2014 RAW READING (at rest, |a| should already be near 1 g)")
    a, g, gstd, temp = average(imu, n=200)
    print(f"    accel = ({a[0]:+.3f}, {a[1]:+.3f}, {a[2]:+.3f}) g   |a| = "
          f"{np.linalg.norm(a):.3f}")
    print(f"    gyro  = ({g[0]:+.2f}, {g[1]:+.2f}, {g[2]:+.2f}) dps   temp = {temp:.1f} \u00b0C")
    if 0.7 < float(np.linalg.norm(a)) < 1.3:
        ok("Uncalibrated |a| near 1 g", f"{np.linalg.norm(a):.3f} g")
        passes += 1
    else:
        bad("Uncalibrated |a| near 1 g", f"{np.linalg.norm(a):.3f} g \u2014 sensor fault?")

    # ---------- STAGE 2: stillness check via gyro ----------
    print("\n  STAGE 2 \u2014 STILLNESS (gyro \u2248 0 when the board is not moving)")
    print(f"    gyro mean |max| = {np.abs(g).max():.2f} dps   std |max| = {gstd.max():.2f} dps")
    if is_still(g, gstd):
        ok("Board is static", "gyro confirms no motion")
        passes += 1
        print("    \u2192 Static is REQUIRED: motion adds linear acceleration and")
        print("      breaks the |g| = 1 assumption every tilt reading relies on.")
    else:
        bad("Board is static", "gyro sees motion \u2014 put it down and rerun")

    # ---------- STAGE 3: calibration ----------
    cal = None
    if os.path.exists(CAL_FILE) and not args.recal:
        cal = json.load(open(CAL_FILE))
        print(f"\n  STAGE 3 \u2014 CALIBRATION (loaded {CAL_FILE}; --recal to redo)")
        for i, nm in enumerate("XYZ"):
            print(f"    {nm}: bias={cal['bias'][i]:+.4f} g   scale={cal['scale'][i]:.4f}")
        ok("Calibration available", "from file")
        passes += 1
    else:
        print("\n  STAGE 3 \u2014 CALIBRATION (+1 g / \u22121 g per axis)")
        print("    Point each axis DOWN (reads +1 g), then UP (reads \u22121 g).")
        print("    bias = midpoint of the two;  scale corrects the span to exactly 2 g.\n")
        bias, scale = [0.0]*3, [1.0]*3
        good = True
        for i, nm in enumerate("XYZ"):
            input(f"  \u2192 Point +{nm} DOWN (toward ground), hold STILL, Enter\u2026")
            ad, gd, gsd, _ = average(imu, n=300)
            if not is_still(gd, gsd):
                warn(f"{nm} +1g capture", "board was moving \u2014 result may be off")
            input(f"  \u2192 Point +{nm} UP (toward sky), hold STILL, Enter\u2026")
            au, gu, gsu, _ = average(imu, n=300)
            if not is_still(gu, gsu):
                warn(f"{nm} \u22121g capture", "board was moving \u2014 result may be off")
            p, n_ = float(ad[i]), float(au[i])
            bias[i] = (p + n_) / 2.0            # midpoint = 0 g offset
            span = abs(p - n_) / 2.0            # should be 1.0 g
            scale[i] = 1.0 / span if span > 1e-6 else 1.0
            print(f"    {nm}: +1g={p:+.3f}  \u22121g={n_:+.3f}  \u2192 bias={bias[i]:+.4f} g  "
                  f"scale={scale[i]:.4f}")
            if not (0.80 < span < 1.20):
                good = False
                print(f"    \u2192 span {span:.3f} g is off \u2014 axis may not have been vertical")
        cal = {"bias": bias, "scale": scale, "addr": args.addr}
        json.dump(cal, open(CAL_FILE, "w"), indent=2)
        if good:
            ok("Calibration plausible", f"saved \u2192 {CAL_FILE}")
            passes += 1
        else:
            bad("Calibration plausible", "redo, keeping each axis truly vertical")

    bias = np.array(cal["bias"], dtype=float)
    scale = np.array(cal["scale"], dtype=float)

    # ---------- STAGE 4: |g| == 1 in ANY pose ----------
    print("\n  STAGE 4 \u2014 GRAVITY MAGNITUDE  (|g| must be \u22481.00 in ANY orientation)")
    print("    The core check: at rest the sensor sees ONLY gravity, whose")
    print("    magnitude is invariant \u2014 so this catches calibration errors that")
    print("    no single-orientation test would reveal.")
    mags = []
    for k in range(3):
        input(f"  \u2192 Any orientation ({k+1}/3), hold STILL, Enter\u2026")
        a, gy, gs, _ = average(imu, n=300)
        if not is_still(gy, gs):
            warn("pose capture", "board moving \u2014 |g| will be wrong")
        gv = apply_cal(a, bias, scale)
        r, p, t, norm = tilt_from_g(gv)
        mags.append(norm)
        print(f"    g = ({gv[0]:+.3f}, {gv[1]:+.3f}, {gv[2]:+.3f})   |g| = {norm:.3f}")
    mags = np.array(mags)
    if np.all(np.abs(mags - 1.0) < 0.06):
        ok("|g| \u2248 1.00 in all poses", f"range {mags.min():.3f}\u2013{mags.max():.3f}")
        passes += 1
    else:
        bad("|g| \u2248 1.00 in all poses", f"range {mags.min():.3f}\u2013{mags.max():.3f}")
        print("    \u2192 Rerun calibration with --recal, keeping each axis truly vertical.")

    # ---------- STAGE 5: gravity DIRECTION / level ----------
    print("\n  STAGE 5 \u2014 GRAVITY DIRECTION (flat & level \u2192 gravity lands on Z)")
    input("  \u2192 Lay the board FLAT and LEVEL (bubble level), hold STILL, Enter\u2026")
    a, gy, gs, temp = average(imu, n=400)
    gv = apply_cal(a, bias, scale)
    r, p, t, norm = tilt_from_g(gv)
    print(f"    g = ({gv[0]:+.3f}, {gv[1]:+.3f}, {gv[2]:+.3f})   |g| = {norm:.3f}")
    print(f"    roll  = {r:+.2f}\u00b0")
    print(f"    pitch = {p:+.2f}\u00b0")
    print(f"    TILT FROM VERTICAL = {t:.2f}\u00b0    (0\u00b0 = perfectly level)")
    if abs(gv[2]) > 0.94 and abs(gv[0]) < 0.20 and abs(gv[1]) < 0.20:
        ok("Gravity aligns with Z when level", f"Z = {gv[2]:+.3f} g")
        passes += 1
        if gv[2] < 0:
            print("    (Z negative \u2192 board inverted vs assumed axis. Fine \u2014 record")
            print("     the sign convention so the field data is unambiguous.)")
    else:
        bad("Gravity aligns with Z when level",
            f"X={gv[0]:+.2f} Y={gv[1]:+.2f} Z={gv[2]:+.2f}")
        print("    \u2192 1 g on a horizontal axis \u21d2 the IMU is mounted on its side")
        print("      relative to the board. Note the mounting rotation and correct it.")

    print("\n  " + "\u2500" * 56)
    print(f"  {passes}/{total} stages passed")
    if passes == total:
        print("  \u2713 Tilt is a trustworthy absolute reference.")
        print("    Pair it with ONE Sun fix \u2192 full frame orientation (azimuth + tilt).")
    else:
        print("  \u2717 Fix flagged stages before trusting tilt in the field.")

    # ---------- LIVE ----------
    print("\n  LIVE TILT READOUT (Ctrl+C to stop)  \u2014 also flags if the frame MOVES")
    try:
        while True:
            a, gy, gs, temp = average(imu, n=20, dt=0.002)
            gv = apply_cal(a, bias, scale)
            r, p, t, n_ = tilt_from_g(gv)
            moving = "" if is_still(gy, gs, 3.0) else "  << MOVING"
            lvl = " LEVEL" if t < 1.0 else "      "
            print(f"   g=({gv[0]:+.2f},{gv[1]:+.2f},{gv[2]:+.2f}) |g|={n_:.2f}  "
                  f"roll={r:+6.1f}\u00b0 pitch={p:+6.1f}\u00b0 tilt={t:5.1f}\u00b0{lvl}"
                  f"  {temp:.1f}\u00b0C{moving}")
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\n  Live view stopped.")
    finally:
        imu.close()


if __name__ == "__main__":
    main()
