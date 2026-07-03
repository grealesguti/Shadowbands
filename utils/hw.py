#!/usr/bin/env python3
"""
utils/hw.py
────────────
Hardware abstraction — confirmed working on:
  Pi Zero W + Raspbian Trixie
  BMP180 (I²C 0x77), DS3231 (I²C 0x68)
  ADXL335 (analog via MCP3208 CH5/6/7)
  OV5647 IR camera via rpicam-still / rpicam-vid

Camera notes:
  - On Trixie use rpicam-* NOT libcamera-*
  - vcgencmd get_camera shows detected=0 even when working — ignore it
  - Correct check: rpicam-hello --list-cameras
  - OV5647 appears as UU at 0x36 on i2cdetect bus 10 — normal
"""

import os, sys, time, json, logging, statistics, subprocess
from pathlib import Path

# ── Config ────────────────────────────────────────────────────
try:
    import yaml
    def load_config(path=None):
        p = Path(path) if path else \
            Path(__file__).parent.parent / "config" / "config.yaml"
        with open(p) as f:
            return yaml.safe_load(f)
except ImportError:
    def load_config(path=None):
        return {
            "hardware": {
                "n_opt101": 2, "sensor_spacing_m": 0.05,
                "spi_bus": 0, "spi_ce_mcp3208_1": 0, "spi_hz": 1_000_000,
                "i2c_bus": 1, "bmp180_address": 0x77, "ds3231_address": 0x68,
                "adxl335_ch_x": 5, "adxl335_ch_y": 6, "adxl335_ch_z": 7,
                "adxl335_vcc": 3.3, "led_sync_gpio": 17,
                "gps_available": False,
                "camera_available": True, "camera_cmd": "rpicam",
                "camera_width": 1920, "camera_height": 1080,
                "camera_fps": 30, "camera_suppress_log": True,
            },
            "calibration": {
                "cal_file": "/home/eclipse/shadowband_cal.json",
                "dark_samples": 64, "uniformity_samples": 64,
                "max_sensitivity_deviation_pct": 20.0,
            },
            "recording": {
                "output_dir": "/home/eclipse/data",
                "adc_target_sps": 150, "atmos_hz": 1,
                "write_buffer_kb": 128,
                "pre_contact2_s": 180, "post_contact3_s": 180,
            },
            "eclipse": {
                "contact2_utc": "2026-08-12T18:23:45Z",
                "contact3_utc": "2026-08-12T18:27:12Z",
            },
            "analysis": {
                "min_band_velocity_ms": 0.5, "max_band_velocity_ms": 15.0,
                "xcorr_max_lag_s": 0.5,
                "bandpass_low_hz": 0.1, "bandpass_high_hz": 5.0,
                "min_snr_db": 6.0,
            },
            "logging": {"level": "INFO", "console": True,
                        "log_file": "/home/eclipse/data/shadowband.log"},
        }

# ── Logging ───────────────────────────────────────────────────
def setup_logging(cfg=None):
    cfg  = cfg or load_config()
    lcfg = cfg.get("logging", {})
    level = getattr(logging, lcfg.get("level", "INFO"), logging.INFO)
    handlers = []
    if lcfg.get("console", True):
        handlers.append(logging.StreamHandler(sys.stdout))
    lf = lcfg.get("log_file")
    if lf:
        Path(lf).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(lf))
    logging.basicConfig(level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S", handlers=handlers)
    return logging.getLogger("shadowband")

# ── Optional imports ──────────────────────────────────────────
try:
    import spidev as _spidev
    _SPI_OK = True
except ImportError:
    _SPI_OK = False

try:
    import smbus2 as _smbus2
    _SMBUS_OK = True
except ImportError:
    _SMBUS_OK = False

try:
    import adafruit_ds3231 as _ds3231_lib
    import board as _board, busio as _busio
    _DS_OK = True
except ImportError:
    _DS_OK = False

# ── Camera ────────────────────────────────────────────────────
def detect_camera_stack():
    """
    Detect which camera command is available.
    On Raspbian Trixie: rpicam-* is the correct stack.
    Returns 'rpicam', 'libcamera', or None.
    """
    for cmd in ["rpicam-still", "libcamera-still"]:
        r = subprocess.run(["which", cmd], capture_output=True, timeout=3)
        if r.returncode == 0:
            return "rpicam" if "rpicam" in cmd else "libcamera"
    return None

def camera_is_present():
    """
    Check if camera is detected.
    NOTE: vcgencmd get_camera is unreliable on Trixie.
    Use rpicam-hello --list-cameras instead.
    """
    stack = detect_camera_stack()
    if stack is None:
        return False
    cmd = f"{stack}-hello"
    try:
        r = subprocess.run([cmd, "--list-cameras"],
                           capture_output=True, text=True, timeout=10)
        return "ov5647" in r.stdout.lower() or \
               "imx" in r.stdout.lower() or \
               "Available cameras" in r.stdout
    except Exception:
        return False

def capture_still(out_path, width=1920, height=1080,
                  stack="rpicam", suppress_log=True):
    """Capture a JPEG still. Returns True on success."""
    stderr = subprocess.DEVNULL if suppress_log else None
    cmd = [f"{stack}-still",
           "-o", str(out_path),
           "--width", str(width),
           "--height", str(height),
           "--timeout", "2000",
           "--nopreview"]
    try:
        r = subprocess.run(cmd, capture_output=suppress_log,
                           stderr=stderr, timeout=15)
        return Path(out_path).exists() and \
               Path(out_path).stat().st_size > 1000
    except Exception:
        return False

def start_video(out_path, width=1920, height=1080,
                fps=30, duration_s=300, stack="rpicam",
                suppress_log=True):
    """
    Start video recording as a background subprocess.
    Returns the Popen process object.
    Kill with proc.terminate() when done.
    """
    stderr = subprocess.DEVNULL if suppress_log else None
    cmd = [f"{stack}-vid",
           "-o", str(out_path),
           "--width",     str(width),
           "--height",    str(height),
           "--framerate", str(fps),
           "--timeout",   str(duration_s * 1000),
           "--nopreview",
           "--codec",     "h264"]
    try:
        return subprocess.Popen(cmd,
                                stdout=subprocess.DEVNULL,
                                stderr=stderr)
    except FileNotFoundError:
        return None

# ── MCP3208 ───────────────────────────────────────────────────
class MCP3208:
    def __init__(self, bus=0, ce=0, hz=1_000_000, simulate=False):
        self.log  = logging.getLogger("shadowband.mcp3208")
        self._sim = simulate or not _SPI_OK
        if not self._sim:
            try:
                self._spi = _spidev.SpiDev()
                self._spi.open(bus, ce)
                self._spi.max_speed_hz = hz
                self._spi.mode = 0
                self.log.info("MCP3208 bus%d CE%d @ %d Hz", bus, ce, hz)
            except Exception as e:
                self.log.warning("MCP3208 failed: %s — simulation", e)
                self._sim = True
        else:
            self.log.warning("MCP3208 simulation mode")

    def read(self, ch):
        if self._sim:
            import random, math
            return max(0, min(4095,
                int(2000 + 800*math.sin(time.time()*0.2+ch*0.8)
                    + random.gauss(0, 20))))
        r = self._spi.xfer2([0x06|(ch>>2),(ch&3)<<6,0x00])
        return ((r[1]&0x0F)<<8)|r[2]

    def read_avg(self, ch, n=64):
        vals = [self.read(ch) for _ in range(n)]
        return statistics.mean(vals), statistics.stdev(vals) if n>1 else 0.0

    def read_all(self, n_ch=8):
        return [self.read(c) for c in range(n_ch)]

    def close(self):
        if not self._sim:
            try: self._spi.close()
            except: pass

# ── ADXL335 ───────────────────────────────────────────────────
class ADXL335:
    SENSITIVITY = 0.330   # V/g
    ZERO_G_RATIO = 0.5    # VCC/2 at 0g

    def __init__(self, adc, ch_x=5, ch_y=6, ch_z=7, vcc=3.3):
        self._adc  = adc
        self._chs  = {"x": ch_x, "y": ch_y, "z": ch_z}
        self._vcc  = vcc
        self._zero = vcc * self.ZERO_G_RATIO

    def _to_g(self, raw):
        v = raw * self._vcc / 4096.0
        return (v - self._zero) / self.SENSITIVITY

    def read(self):
        return {ax: round(self._to_g(self._adc.read(ch)), 4)
                for ax, ch in self._chs.items()}

    def read_raw(self):
        return {ax: self._adc.read(ch) for ax, ch in self._chs.items()}

# ── BMP180 ────────────────────────────────────────────────────
class BMP180:
    """I²C pressure + temperature. Address 0x77. No humidity."""

    def __init__(self, address=0x77, simulate=False):
        self.log  = logging.getLogger("shadowband.bmp180")
        self._sim = simulate or not _SMBUS_OK
        self._addr = address
        if not self._sim:
            try:
                self._bus = _smbus2.SMBus(1)
                self._cal = self._read_cal()
                self.log.info("BMP180 @ 0x%02X OK", address)
            except Exception as e:
                self.log.warning("BMP180: %s — simulation", e)
                self._sim = True

    def _read_cal(self):
        import struct
        d = self._bus.read_i2c_block_data(self._addr, 0xAA, 22)
        return struct.unpack(">hhhHHHhhhhh", bytes(d))

    def _raw_temp(self):
        self._bus.write_byte_data(self._addr, 0xF4, 0x2E)
        time.sleep(0.005)
        d = self._bus.read_i2c_block_data(self._addr, 0xF6, 2)
        return (d[0]<<8)|d[1]

    def _raw_pres(self, oss=1):
        self._bus.write_byte_data(self._addr, 0xF4, 0x34|(oss<<6))
        time.sleep(0.008)
        d = self._bus.read_i2c_block_data(self._addr, 0xF6, 3)
        return ((d[0]<<16)|(d[1]<<8)|d[2])>>(8-oss)

    def read(self):
        if self._sim:
            import random
            return {"temp_c": round(22+random.gauss(0,.2),2),
                    "pressure_hpa": round(1013+random.gauss(0,.2),2),
                    "humidity_pct": None}
        try:
            AC1,AC2,AC3,AC4,AC5,AC6,B1,B2,MB,MC,MD = self._cal
            UT = self._raw_temp()
            X1 = ((UT-AC6)*AC5)>>15
            X2 = (MC<<11)//(X1+MD)
            B5 = X1+X2
            temp = ((B5+8)>>4)/10.0
            UP = self._raw_pres(1)
            B6=B5-4000; X1=(B2*(B6*B6>>12))>>11; X2=AC2*B6>>11
            X3=X1+X2; B3=(((AC1*4+X3)<<1)+2)>>2
            X1=AC3*B6>>13; X2=(B1*(B6*B6>>12))>>16
            X3=((X1+X2)+2)>>2; B4=AC4*(X3+32768)>>15
            B7=(UP-B3)*(50000>>1)
            p=int((B7/B4)*2) if B7<0x80000000 else int((B7*2)/B4)
            X1=(p>>8)**2; X1=(X1*3038)>>16; X2=(-7357*p)>>16
            p=p+((X1+X2+3791)>>4)
            return {"temp_c": round(temp,2),
                    "pressure_hpa": round(p/100.0,2),
                    "humidity_pct": None}
        except Exception as e:
            self.log.error("BMP180 read: %s", e)
            return {"temp_c":None,"pressure_hpa":None,"humidity_pct":None}

# ── DS3231 ────────────────────────────────────────────────────
class DS3231:
    def __init__(self, simulate=False):
        self.log  = logging.getLogger("shadowband.ds3231")
        self._sim = simulate or not _DS_OK
        if not self._sim:
            try:
                i2c = _busio.I2C(_board.SCL, _board.SDA)
                self._dev = _ds3231_lib.DS3231(i2c)
                self.log.info("DS3231 OK")
            except Exception as e:
                self.log.warning("DS3231: %s — simulation", e)
                self._sim = True

    def read(self):
        if self._sim:
            import datetime
            n = datetime.datetime.now()
            return {"year":n.year,"month":n.month,"day":n.day,
                    "hour":n.hour,"minute":n.minute,"second":n.second,
                    "temperature_c":25.0,"lost_power":False}
        t = self._dev.datetime
        return {"year":t.tm_year,"month":t.tm_mon,"day":t.tm_mday,
                "hour":t.tm_hour,"minute":t.tm_min,"second":t.tm_sec,
                "temperature_c":round(self._dev.temperature,1),
                "lost_power":self._dev.lost_power}

    def sync_from_system(self):
        if self._sim: return
        import time as _t
        self._dev.datetime = _t.localtime()
        self.log.info("DS3231 synced from system clock")

# ── Hardware composite ────────────────────────────────────────
class Hardware:
    def __init__(self, cfg=None, simulate=False):
        self.cfg      = cfg or load_config()
        self.simulate = simulate
        self.log      = logging.getLogger("shadowband.hw")
        hcfg          = self.cfg["hardware"]
        self._n_ch    = hcfg["n_opt101"]
        self._dark    = [0.0]*self._n_ch
        self._factor  = [1.0]*self._n_ch

        self.adc   = MCP3208(bus=hcfg["spi_bus"],
                             ce=hcfg["spi_ce_mcp3208_1"],
                             hz=hcfg["spi_hz"],
                             simulate=simulate)
        self.accel = ADXL335(self.adc,
                             ch_x=hcfg["adxl335_ch_x"],
                             ch_y=hcfg["adxl335_ch_y"],
                             ch_z=hcfg["adxl335_ch_z"],
                             vcc=hcfg["adxl335_vcc"])
        self.bmp   = BMP180(address=hcfg["bmp180_address"],
                            simulate=simulate)
        self.rtc   = DS3231(simulate=simulate)

        # Camera stack detection
        self._cam_stack = hcfg.get("camera_cmd", "rpicam")
        self._cam_suppress = hcfg.get("camera_suppress_log", True)
        self._load_calibration()

    def _load_calibration(self):
        cal_file = self.cfg["calibration"]["cal_file"]
        try:
            with open(cal_file) as f:
                cal = json.load(f)
            n = self._n_ch
            self._dark   = [cal["dark"][f"ch{c}"]["mean"]         for c in range(n)]
            self._factor = [cal["sensitivity"][f"ch{c}"]["factor"] for c in range(n)]
            self.log.info("Calibration loaded (%s)", cal.get("date","?"))
        except FileNotFoundError:
            self.log.warning("No calibration — using unity. "
                             "Run: python3 calibration/run_calibration.py")
        except Exception as e:
            self.log.error("Calibration load: %s", e)

    def read_raw(self, ch):
        return self.adc.read(ch)

    def read_all_raw(self):
        return self.adc.read_all(self._n_ch)

    def read_all_cal(self):
        raws = self.read_all_raw()
        return [(raws[c]-self._dark[c])*self._factor[c]
                for c in range(self._n_ch)]

    def read_atmosphere(self):
        atm = self.bmp.read()
        acc = self.accel.read()
        atm.update({f"accel_{k}": v for k,v in acc.items()})
        return atm

    def capture_still(self, out_path, width=None, height=None):
        hcfg = self.cfg["hardware"]
        return capture_still(
            out_path,
            width  = width  or hcfg["camera_width"],
            height = height or hcfg["camera_height"],
            stack  = self._cam_stack,
            suppress_log = self._cam_suppress)

    def start_video(self, out_path, duration_s=300):
        hcfg = self.cfg["hardware"]
        return start_video(
            out_path,
            width      = hcfg["camera_width"],
            height     = hcfg["camera_height"],
            fps        = hcfg["camera_fps"],
            duration_s = duration_s,
            stack      = self._cam_stack,
            suppress_log = self._cam_suppress)

    def close(self):
        self.adc.close()
