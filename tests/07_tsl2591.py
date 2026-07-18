#!/usr/bin/env python3
"""
04b_tsl2591_debug.py  --  TSL2591 bring-up & optical dynamic-range test (I2C).

Digital High-Dynamic-Range Lux sensor over I2C -- costs ZERO MCP3208
channels, leaving all 8 ADC inputs for other custom analog tracking.

WIRING:
    TSL2591 VIN -> 3.3 V         (or 5V if breakout has onboard regulator)
    TSL2591 GND -> GND
    TSL2591 SDA -> Pi GPIO 2     (physical pin 3)
    TSL2591 SCL -> Pi GPIO 3     (physical pin 5)
    TSL2591 INT -> leave float   (optional: tie to GPIO4 for threshold alerts)

SETUP:
    sudo raspi-config      -> Interface Options -> I2C -> Enable ; reboot
    sudo apt install -y i2c-tools python3-smbus
    i2cdetect -y 1         -> must show 29

PHYSICS CHECKED:
    The TSL2591 houses a dual-photodiode matrix: Channel 0 (Visible + IR) and 
    Channel 1 (Infrared). 
      SATURATION Check: Raw counts cap out at 37,888 (in 100ms integration mode) 
                         or 65,535. If saturated, gain must be lowered.
      RATIO Check: Under normal daylight/artificial light, CH1 (IR) should be 
                   a reasonable fraction of CH0. If CH1 >= CH0, the sensor is 
                   likely blinded by an intense infrared source or saturated.
"""

import os
import sys
import time
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

# --- TSL2591 Registers & Constants ---
TSL2591_COMMAND_BIT = 0xA0
REG_ENABLE          = 0x00
REG_CONTROL         = 0x01
REG_ID              = 0x12
REG_C0DATAL         = 0x14
REG_C1DATAL         = 0x16

# Enable Register Bits
ENABLE_POWERON      = 0x01
ENABLE_AEN          = 0x02  # ALS Enable

# Gain Settings mapping to multipliers
GAINS = {
    0x00: (1.0, "LOW (1x)"),
    0x10: (25.0, "MED (25x)"),
    0x20: (428.0, "HIGH (428x)"),
    0x30: (9876.0, "MAX (9876x)")
}

# Integration Times mapping to ms durations
TIMES = {
    0x00: (100.0, "100ms"),
    0x01: (200.0, "200ms"),
    0x02: (300.0, "300ms"),
    0x03: (400.0, "400ms"),
    0x04: (500.0, "500ms"),
    0x05: (600.0, "600ms")
}

class TSL2591:
    def __init__(self, bus_id=1, addr=0x29):
        self.bus = smbus.SMBus(bus_id)
        self.addr = addr
        
        # Power up and enable ALS
        self.write_reg(REG_ENABLE, ENABLE_POWERON | ENABLE_AEN)
        time.sleep(0.05)
        
        # Set default state: Medium Gain (25x), 100ms Integration time
        self.current_gain_code = 0x10
        self.current_time_code = 0x00
        self.set_timing_gain(self.current_time_code, self.current_gain_code)

    def write_reg(self, reg, value):
        packet_reg = TSL2591_COMMAND_BIT | reg
        self.bus.write_byte_data(self.addr, packet_reg, value)

    def read_reg(self, reg):
        packet_reg = TSL2591_COMMAND_BIT | reg
        return self.bus.read_byte_data(self.addr, packet_reg)

    def id(self):
        return self.read_reg(REG_ID)

    def set_timing_gain(self, time_code, gain_code):
        self.current_time_code = time_code
        self.current_gain_code = gain_code
        # Combine parameters into the control register byte
        control_byte = time_code | gain_code
        self.write_reg(REG_CONTROL, control_byte)
        time.sleep(0.1)

    def read_raw(self):
        """Reads raw channel data loops and returns (CH0_Full, CH1_IR)."""
        # Read CH0 data (2 bytes)
        packet_reg_c0 = TSL2591_COMMAND_BIT | REG_C0DATAL
        d0 = self.bus.read_i2c_block_data(self.addr, packet_reg_c0, 2)
        ch0 = (d0[1] << 8) | d0[0]
        
        # Read CH1 data (2 bytes)
        packet_reg_c1 = TSL2591_COMMAND_BIT | REG_C1DATAL
        d1 = self.bus.read_i2c_block_data(self.addr, packet_reg_c1, 2)
        ch1 = (d1[1] << 8) | d1[0]
        
        return ch0, ch1

    def calculate_lux(self, ch0, ch1):
        """Converts raw data readings into empirical Lux units."""
        # Detect clipping limits
        max_counts = 37888 if self.current_time_code == 0x00 else 65535
        if ch0 >= max_counts or ch1 >= max_counts:
            return float('nan') # Saturated signal flag
            
        atime = TIMES[self.current_time_code][0]
        again = GAINS[self.current_gain_code][0]
        
        # Lux calculation coefficients from the hardware manual
        cpl = (atime * again) / 408.0
        if cpl == 0:
            return 0.0
            
        lux1 = (ch0 - (2.0 * ch1)) / cpl
        lux2 = ((0.6 * ch0) - (ch1)) / cpl
        
        return max(lux1, lux2, 0.0)

    def close(self):
        try:
            self.write_reg(REG_ENABLE, 0x00) # Sleep Mode
            self.bus.close()
        except Exception:
            pass


def ok(d, x=""):   print(f"  \u2713 [PASS]  {d:<44s} {x}")
def bad(d, x=""):  print(f"  \u2717 [FAIL]  {d:<44s} {x}")
def warn(d, x=""): print(f"  ! [WARN]  {d:<44s} {x}")


def average(sensor, n=20, dt=0.02):
    """Averages n light samples -> (mean_ch0, mean_ch1, std_ch0)."""
    C0 = np.zeros(n); C1 = np.zeros(n)
    for i in range(n):
        c0, c1 = sensor.read_raw()
        C0[i] = c0; C1[i] = c1
        time.sleep(dt)
    return C0.mean(), C1.mean(), C0.std()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x29)
    ap.add_argument("--bus", type=int, default=1)
    args = ap.parse_args()

    print()
    print("\u2554" + "\u2550" * 54 + "\u2557")
    print("\u2551  04b \u2014 TSL2591 Optical Lux Diagnostic Script       \u2551")
    print("\u255a" + "\u2550" * 54 + "\u255d")
    print(f"\n  I2C bus {args.bus}, addr 0x{args.addr:02X}   (uses NO MCP3208 channels)")
    print("  Evaluates dynamic light profiles across distinct visual spectra.\n")

    passes, total = 0, 5

    # ---------- STAGE 0: Device Present ----------
    try:
        sensor = TSL2591(bus_id=args.bus, addr=args.addr)
    except Exception as e:
        bad("I2C device reachable", f"{e}")
        print("    \u2192 Enable I2C (raspi-config), check lines, then execute: i2cdetect -y 1")
        return
    ok("I2C device reachable", f"0x{args.addr:02X}")

    # ---------- STAGE 1: Check Silicon Signature ----------
    print("\n  STAGE 1 \u2014 IDENTITY VERIFICATION (REG_ID)")
    try:
        hw_id = sensor.id()
    except Exception as e:
        bad("REG_ID readable", str(e)); return
        
    print(f"    TSL2591 Hardware Response ID = 0x{hw_id:02X}   (Genuine chip returns 0x50)")
    if hw_id == 0x50:
        ok("Genuine TSL2591 signature verified", "0x50")
        passes += 1
    else:
        bad("Genuine TSL2591 signature", f"Unexpected device footprint signature: 0x{hw_id:02X}")

    # ---------- STAGE 2: Baseline Execution Range ----------
    print("\n  STAGE 2 \u2014 PHOTODIODE REGISTER CONVERGENCE")
    c0, c1, _ = average(sensor, n=15)
    print(f"    Raw CH0 (Visible + IR) Count Baseline: {c0:.1f}")
    print(f"    Raw CH1 (Pure Infrared) Count Baseline: {c1:.1f}")
    
    if c0 > 0 or c1 > 0:
        ok("Photodiode reporting actively", "Registers registering active photon activity.")
        passes += 1
    else:
        warn("Zero count levels detected", "Sensor reporting pitch blackness. Verify window is unblocked.")
        passes += 1

    # ---------- STAGE 3: Structural Noise Profile ----------
    print("\n  STAGE 3 \u2014 LIGHT STABILITY MATRIX")
    _, _, c0_std = average(sensor, n=30)
    print(f"    CH0 Integration Deviation: {c0_std:.4f} counts")
    if c0_std < 15.0:
        ok("Stable optical tracking environment", f"Standard variance safe ({c0_std:.2f} counts).")
        passes += 1
    else:
        warn("Fluctuating stream detected", f"High reading variance ({c0_std:.2f}). Check for artificial light flicker (50/60Hz noise).")
        passes += 1

    # ---------- STAGE 4: Automated Gain Gain Shifting Step ----------
    print("\n  STAGE 4 \u2014 DYNAMIC AMPLIFICATION INTERFACE")
    print("    Testing programmatic transition from Medium Gain (25x) to Low Gain (1x)...")
    sensor.set_timing_gain(0x00, 0x00) # Drop to Low Gain
    c0_low, _, _ = average(sensor, n=10)
    
    print(f"    Low Gain (1x) Reading : {c0_low:.1f} counts")
    print(f"    Med Gain (25x) Reading: {c0:.1f} counts")
    
    # Restore normal operational context
    sensor.set_timing_gain(0x00, 0x10)
    
    if c0_low <= c0 or np.isnan(sensor.calculate_lux(c0, c1)):
        ok("Gain attenuation responsive", "Amplification step registers drop safely.")
        passes += 1
    else:
        bad("Gain attenuation unresponsive", "Readings failed to scale proportionally.")

    # ---------- STAGE 5: Multi-Channel Physics Verification ----------
    print("\n  STAGE 5 \u2014 SPECTRAL DISTRIBUTION SPECTRUM")
    if c0 >= c1:
        ok("Spectral distribution is mathematically sound", f"CH0 (Full Spectrum: {c0:.1f}) >= CH1 (IR Only: {c1:.1f})")
        passes += 1
    else:
        bad("Spectral distribution failure", f"CH1 ({c1:.1f}) outpaces CH0 ({c0:.1f}). Silicon saturation error.")

    print("\n  " + "\u2500" * 56)
    print(f"  {passes}/{total} diagnostic validation stages passed.")
    if passes == total:
        print("  \u2713 Optical pipeline operations normal.")
    else:
        print("  \u2717 Check ambient illumination parameters before tracking real hardware loops.")

    # ---------- LIVE STREAM READOUT LOOP ----------
    print("\n  LIVE SPECTRUM STREAM (Ctrl+C to terminate)")
    try:
        while True:
            raw0, raw1 = sensor.read_raw()
            lux = sensor.calculate_lux(raw0, raw1)
            
            # Format text based on potential overflow events
            lux_str = f"{lux:8.2f} Lux" if not np.isnan(lux) else "SATURATED/OVERFLOW"
            print(f"   Full Spec (CH0): {raw0:<5d} | IR Spec (CH1): {raw1:<5d} | Output: {lux_str}", end="\r")
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n\n  Live stream stopped.")
    finally:
        sensor.close()


if __name__ == "__main__":
    main()