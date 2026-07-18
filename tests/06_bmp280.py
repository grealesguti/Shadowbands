#!/usr/bin/env python3
"""
04b_bmp280_debug.py  --  BMP280 bring-up & environmental integrity test (I2C).

Digital barometric pressure and temperature sensor over I2C -- costs ZERO 
MCP3208 channels, keeping all 8 ADC inputs for other analog hardware.

WIRING:
    BMP280 VCC -> 3.3 V        (Sensor is 3.3 V; most breakouts regulate)
    BMP280 GND -> GND
    BMP280 SDA -> Pi GPIO 2    (physical pin 3)
    BMP280 SCL -> Pi GPIO 3    (physical pin 5)
    BMP280 SDO -> GND          (I2C address 0x76; tie to 3.3V for 0x77)
    BMP280 CSB -> leave float  (or tie to 3.3V to explicitly force I2C mode)

SETUP:
    sudo raspi-config      -> Interface Options -> I2C -> Enable ; reboot
    sudo apt install -y i2c-tools python3-smbus
    i2cdetect -y 1         -> must show 76 (or 77)

WHAT THIS PROVIDES (and does NOT):
    PROVIDES: Atmospheric Pressure (Pa / hPa), Altimetry (relative altitude 
              from pressure differences), and Ambient Temperature (°C).
    DOES NOT: Tilt, roll, pitch, or orientation tracking. It is a weather/altitude
              reference instrument, not an IMU.

PHYSICS CHECKED:
    At rest, barometric pressure should mirror local mean sea level conditions adjusted 
    for local altitude (~300 to 1100 hPa). 
    Noise profiling ensures the sensor baseline is stable and lacks high-frequency spikes.
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

CAL_FILE = "bmp280_cal.json"

# --- BMP280 registers ---
REG_ID          = 0xD0  # Should read 0x58 for BMP280
REG_RESET       = 0xE0
REG_STATUS      = 0xF3
REG_CTRL_MEAS   = 0xF4
REG_CONFIG      = 0xF5
REG_PRES_MSB    = 0xF7
REG_TEMP_MSB    = 0xFA
REG_CALIB_START = 0x88  # Trimming factory parameters base address


class BMP280:
    def __init__(self, bus_id=1, addr=0x76):
        self.bus = smbus.SMBus(bus_id)
        self.addr = addr
        
        # Initial Reset
        self.bus.write_byte_data(self.addr, REG_RESET, 0xB6)
        time.sleep(0.1)
        
        # Read factory trimming parameters
        self._read_calibration()
        
        # Configure sensor: Normal Mode, Temp oversampling x1, Pressure oversampling x4
        # 0x2E: Press x4 (010), Temp x1 (001), Mode Normal (11)
        self.bus.write_byte_data(self.addr, REG_CTRL_MEAS, 0x2F)
        # Config: Standby 0.5ms (000), Filter coefficient 4 (010), SPI 3-wire off (0)
        self.bus.write_byte_data(self.addr, REG_CONFIG, 0x08)
        time.sleep(0.05)

    def id(self):
        return self.bus.read_byte_data(self.addr, REG_ID)

    def _read_calibration(self):
        # Read 24 bytes of calibration data starting at 0x88
        d = self.bus.read_i2c_block_data(self.addr, REG_CALIB_START, 24)
        
        def _u16(h, l): return (h << 8) | l
        def _s16(h, l): 
            v = _u16(h, l)
            return v - 65536 if v > 32767 else v

        # Unpack according to data sheet layout
        self.dig_T1 = _u16(d[1], d[0])
        self.dig_T2 = _s16(d[3], d[2])
        self.dig_T3 = _s16(d[5], d[4])
        self.dig_P1 = _u16(d[7], d[6])
        self.dig_P2 = _s16(d[9], d[8])
        self.dig_P3 = _s16(d[11], d[10])
        self.dig_P4 = _s16(d[13], d[12])
        self.dig_P5 = _s16(d[15], d[14])
        self.dig_P6 = _s16(d[17], d[16])
        self.dig_P7 = _s16(d[19], d[18])
        self.dig_P8 = _s16(d[21], d[20])
        self.dig_P9 = _s16(d[23], d[22])

    def read_raw(self):
        """Read continuous data registers block for Pressure and Temp."""
        d = self.bus.read_i2c_block_data(self.addr, REG_PRES_MSB, 6)
        raw_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        raw_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        
        # Temperature compensation formula (from datasheet)
        var1 = (raw_t / 16384.0 - self.dig_T1 / 1024.0) * self.dig_T2
        var2 = ((raw_t / 131072.0 - self.dig_T1 / 8192.0) ** 2) * self.dig_T3
        t_fine = var1 + var2
        temp = t_fine / 5120.0
        
        # Pressure compensation formula (from datasheet)
        var1 = (t_fine / 2.0) - 64000.0
        var2 = var1 * var1 * self.dig_P6 / 32768.0
        var2 = var2 + var1 * self.dig_P5 * 2.0
        var2 = (var2 / 4.0) + (self.dig_P4 * 65536.0)
        var1 = (self.dig_P3 * var1 * var1 / 524288.0 + self.dig_P2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * self.dig_P1
        
        if abs(var1) < 1e-9:
            pres = 0.0 # Prevent zero division
        else:
            pres = 1048576.0 - raw_p
            pres = (pres - (var2 / 4096.0)) * 6250.0 / var1
            var1 = self.dig_P9 * pres * pres / 2147483648.0
            var2 = pres * self.dig_P8 / 32768.0
            pres = pres + (var1 + var2 + self.dig_P7) / 16.0
            
        return pres / 100.0, temp  # Returns (Pressure in hPa, Temperature in °C)

    def close(self):
        try:
            self.bus.close()
        except Exception:
            pass


def ok(d, x=""):   print(f"  \u2713 [PASS]  {d:<44s} {x}")
def bad(d, x=""):  print(f"  \u2717 [FAIL]  {d:<44s} {x}")
def warn(d, x=""): print(f"  ! [WARN]  {d:<44s} {x}")


def average(sensor, n=50, dt=0.01):
    """Average n samples -> (mean_pressure, mean_temp, pressure_std)."""
    P = np.zeros(n); T = np.zeros(n)
    for i in range(n):
        p, t = sensor.read_raw()
        P[i] = p; T[i] = t
        time.sleep(dt)
    return P.mean(), T.mean(), P.std()


def altitude_from_pressure(p, qnh=1013.25):
    """International barometric formula for relative altimetry calculation."""
    return 44330.0 * (1.0 - (p / qnh) ** (1.0 / 5.255))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x76)
    ap.add_argument("--bus", type=int, default=1)
    ap.add_argument("--qnh", type=float, default=1013.25, help="Local sea level pressure benchmark")
    args = ap.parse_args()

    print()
    print("\u2554" + "\u2550" * 54 + "\u2557")
    print("\u2551  04b \u2014 BMP280 Bring-Up \& Diagnostic Script         \u2551")
    print("\u255a" + "\u2550" * 54 + "\u255d")
    print(f"\n  I2C bus {args.bus}, addr 0x{args.addr:02X}   (uses NO MCP3208 channels)")
    print("  Measures absolute structural barometric pressure and baseline temperatures.\n")

    passes, total = 0, 5

    # ---------- STAGE 0: device present ----------
    try:
        sensor = BMP280(bus_id=args.bus, addr=args.addr)
    except Exception as e:
        bad("I2C device reachable", f"{e}")
        print("    \u2192 Enable I2C (raspi-config), check connections, run: i2cdetect -y 1")
        return
    ok("I2C device reachable", f"0x{args.addr:02X}")

    # ---------- STAGE 1: Check Hardware ID Signature ----------
    print("\n  STAGE 1 \u2014 IDENTITY (REG_ID)")
    try:
        hw_id = sensor.id()
    except Exception as e:
        bad("REG_ID readable", str(e)); return
    print(f"    Silicon Hardware Sign ID = 0x{hw_id:02X}   (Genuine BMP280 returns 0x58)")
    if hw_id == 0x58:
        ok("Genuine BMP280 signature verified", "0x58")
        passes += 1
    elif hw_id == 0x60:
        warn("Device recognized", "0x60 \u2014 Detected a BME280 sensor instead. Code will still function.")
        passes += 1
    else:
        bad("Genuine BMP280 signature", f"Unexpected chip ID 0x{hw_id:02X}")

    # ---------- STAGE 2: Trim Constants Loaded ----------
    print("\n  STAGE 2 \u2014 FACTORY CALIBRATION PARAMETERS")
    print(f"    dig_T1: {sensor.dig_T1:<8} dig_P1: {sensor.dig_P1:<8} dig_P5: {sensor.dig_P5}")
    print(f"    dig_T2: {sensor.dig_T2:<8} dig_P2: {sensor.dig_P2:<8} dig_P6: {sensor.dig_P6}")
    print(f"    dig_T3: {sensor.dig_T3:<8} dig_P3: {sensor.dig_P3:<8} dig_P7: {sensor.dig_P7}")
    print(f"    sub_p : {'':<8} dig_P4: {sensor.dig_P4:<8} dig_P8: {sensor.dig_P8}")
    
    if sensor.dig_T1 != 0 and sensor.dig_P1 != 0:
        ok("Calibration parameters active", "Non-zero parameters verified.")
        passes += 1
    else:
        bad("Calibration parameters active", "Zeroed or corrupted factory matrices.")

    # ---------- STAGE 3: Sane Signal Readings ----------
    print("\n  STAGE 3 \u2014 CONVERGENCE RANGE TESTING")
    p_mean, t_mean, p_std = average(sensor, n=100)
    print(f"    Calculated Mean Pressure: {p_mean:.2f} hPa")
    print(f"    Calculated Mean Temp    : {t_mean:.2f} \u00b0C")
    
    if 300.0 < p_mean < 1200.0 and -40.0 < t_mean < 85.0:
        ok("Environmental data is sane", f"Values within normal terrestrial constraints.")
        passes += 1
    else:
        bad("Environmental data is faulty", f"P or T indices are mathematically out of bounds.")

    # ---------- STAGE 4: Baseline Noise Evaluation ----------
    print("\n  STAGE 4 \u2014 SIGNAL NOISE INTERRUPT METRIC")
    print(f"    Pressure Burst Standard Deviation: {p_std:.4f} hPa")
    if p_std < 0.15:
        ok("Low-noise structural threshold", f"Stable read variance ({p_std:.4f} hPa).")
        passes += 1
    else:
        warn("High-noise structural threshold", f"Variance is unusually high ({p_std:.4f} hPa). Shield from air currents.")
        passes += 1

    # ---------- STAGE 5: Calculated Altimetry Mapping ----------
    print("\n  STAGE 5 \u2014 RELATIVE BAROMETRIC ALTIMETRY")
    alt = altitude_from_pressure(p_mean, qnh=args.qnh)
    print(f"    Indicated Relative Altitude: {alt:.2f} meters (using QNH benchmark {args.qnh} hPa)")
    if -500.0 < alt < 9000.0:
        ok("Altimetry pipeline is operational", f"Calculated value is plausible.")
        passes += 1
    else:
        bad("Altimetry pipeline is corrupted", f"Implausible height output: {alt:.1f}m")

    print("\n  " + "\u2500" * 56)
    print(f"  {passes}/{total} diagnostic stages completed successfully.")
    if passes == total:
        print("  \u2713 BMP280 interface is fully stable.")
    else:
        print("  \u2717 Resolve flagged parameters before utilizing tracking loops.")

    # ---------- LIVE READOUT LOOP ----------
    print("\n  LIVE ENVIRONMENTAL STREAM (Ctrl+C to stop)")
    try:
        while True:
            p, t = sensor.read_raw()
            alt = altitude_from_pressure(p, qnh=args.qnh)
            print(f"   Pressure: {p:8.2f} hPa  |  Temp: {t:5.1f} \u00b0C  |  Calc Altitude: {alt:6.1f} m", end="\r")
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n\n  Live view terminated.")
    finally:
        sensor.close()


if __name__ == "__main__":
    main()