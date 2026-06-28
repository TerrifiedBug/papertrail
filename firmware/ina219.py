# Minimal INA219 driver for the Waveshare Pico-UPS-B (I2C1, addr 0x43).
# We only need the BUS VOLTAGE register to estimate LiPo charge %.
#
# The pure battery-curve helpers (voltage_to_pct / is_low) live at module top with
# NO hardware dependency, so they import on a host for the logic tests. The I2C
# `INA219` class guards the `machine` import.
#
# Datasheet refs (TI INA219):
#   reg 0x00 = configuration
#   reg 0x02 = bus voltage   (bits 15..3 = value; LSB = 4 mV; bit1 = CNVR, bit0 = OVF)

try:
    from machine import I2C, Pin
    _HW = True
except ImportError:
    _HW = False

_REG_CONFIG = 0x00
_REG_SHUNTVOLTAGE = 0x01
_REG_BUSVOLTAGE = 0x02

# A general "32V / 320mV shunt, 12-bit, continuous" config -- the value itself is
# not critical for a bus-voltage read; reset-default also works. Kept explicit so a
# cold INA219 is in a known continuous-conversion mode.
_CONFIG_32V_2A = 0x399F


def _clamp(lo, hi, v):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


# Typical single-cell LiPo discharge curve (resting / light load): ascending
# (volts, pct) points. Piecewise-linear between them, clamped at the ends. Only a
# default -- config.BATTERY["curve"] overrides it for per-pack calibration.
DEFAULT_LIPO_CURVE = (
    (3.30, 0), (3.45, 5), (3.60, 10), (3.70, 20), (3.75, 30), (3.79, 40),
    (3.83, 50), (3.87, 60), (3.92, 70), (3.97, 80), (4.05, 90), (4.20, 100),
)


def voltage_to_pct(v_bus, v_min, v_max, curve=None):
    """LiPo bus-voltage -> integer 0..100%, clamped. PURE (host-testable).

    With `curve` (an ascending sequence of (volts, pct) points) the mapping follows
    the real LiPo discharge curve by piecewise-linear interpolation: below the first
    point -> its pct, above the last -> its pct. Without a curve it falls back to the
    old rough-linear map between v_min and v_max.
    """
    if curve:
        if v_bus <= curve[0][0]:
            return int(curve[0][1])
        if v_bus >= curve[-1][0]:
            return int(curve[-1][1])
        for i in range(1, len(curve)):
            v1, p1 = curve[i]
            if v_bus <= v1:
                v0, p0 = curve[i - 1]
                frac = (v_bus - v0) / (v1 - v0) if v1 != v0 else 0
                return int(_clamp(0, 100, round(p0 + frac * (p1 - p0))))
        return int(curve[-1][1])            # unreachable; belt-and-braces
    if v_max <= v_min:                      # guard against a bad config
        return 0
    frac = (v_bus - v_min) / (v_max - v_min)
    return int(_clamp(0, 100, round(frac * 100)))


def is_low(pct, low_pct):
    """True when battery is at/below the low threshold. PURE."""
    return pct <= low_pct


def is_on_battery(shunt_mv, charge_sign=1, threshold_mv=2.0):
    """True if running on battery (discharging / unplugged). PURE / host-testable.

    The shunt monitors battery current. When PLUGGED IN the charger supplies the
    load (and tops up the cell), so battery current is >= 0; only when UNPLUGGED
    does the load pull current OUT of the battery. So a clearly-negative (signed by
    ``charge_sign``) shunt beyond ``threshold_mv`` of noise means on-battery.
    ``charge_sign`` (+1/-1) flips the convention to match the board's shunt wiring.
    """
    return (charge_sign * shunt_mv) < -threshold_mv


class INA219:
    """Read-only bus-voltage helper for the UPS-B fuel gauge."""

    def __init__(self, i2c, addr=0x43):
        if not _HW:
            raise RuntimeError("INA219 requires MicroPython hardware")
        self.i2c = i2c
        self.addr = addr
        self._configure()

    @classmethod
    def from_config(cls, battery_cfg):
        """Build straight from config.BATTERY (bus/sda/scl/freq/addr)."""
        if not _HW:
            raise RuntimeError("INA219 requires MicroPython hardware")
        i2c = I2C(battery_cfg["i2c_bus"],
                  sda=Pin(battery_cfg["sda"]),
                  scl=Pin(battery_cfg["scl"]),
                  freq=battery_cfg["freq"])
        return cls(i2c, addr=battery_cfg["addr"])

    def _write_u16(self, reg, value):
        self.i2c.writeto_mem(self.addr, reg, bytes([(value >> 8) & 0xFF, value & 0xFF]))

    def _read_u16(self, reg):
        data = self.i2c.readfrom_mem(self.addr, reg, 2)
        return (data[0] << 8) | data[1]

    def _configure(self):
        try:
            self._write_u16(_REG_CONFIG, _CONFIG_32V_2A)
        except Exception:
            pass                            # reset-default config is also fine

    def bus_voltage(self):
        """Bus voltage in volts (LSB = 4 mV, value in bits 15..3)."""
        raw = self._read_u16(_REG_BUSVOLTAGE)
        return (raw >> 3) * 0.004

    def shunt_mv(self):
        """Shunt voltage in millivolts, SIGNED (LSB = 10 uV). Sign = current
        direction across the shunt -> charge vs discharge on the UPS-B."""
        raw = self._read_u16(_REG_SHUNTVOLTAGE)
        if raw & 0x8000:                    # 16-bit two's complement -> signed
            raw -= 1 << 16
        return raw * 0.01                   # 10 uV LSB -> mV
