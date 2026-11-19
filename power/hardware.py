"""Readers for the two power signals, using the kernel interfaces directly.

- INA219 on an I2C bus (`/dev/i2c-N`): capacitor voltage and current. The
  device is only read. Its configuration and calibration registers are never
  written; current is computed from the shunt-voltage register.
- GPIO 26 on the RP1 GPIO chip (GPIO v2 character device): external power
  present. The UPS drives the line weakly and the Pi's default pull-down
  overrides it, so the line is requested with bias disabled and given time to
  settle.

Each reader's `read()` returns a value or raises `ReadError`. After a failure
the device is closed and reopened on a later call.
"""
import ctypes
import errno
import fcntl
import os
import struct
import time


class ReadError(Exception):
    pass


# ----------------------------------------------------------------------- INA219

I2C_SLAVE = 0x0703
I2C_SMBUS = 0x0720
I2C_SMBUS_READ = 1
I2C_SMBUS_WORD_DATA = 3

REG_CONFIG, REG_SHUNT, REG_BUS = 0x00, 0x01, 0x02
SHUNT_LSB_V = 10e-6      # shunt register: signed, 10 uV per bit
BUS_LSB_V = 0.004        # bus register: bits 15..3, 4 mV per bit
MODE_CONTINUOUS = 0x7    # config bits 2..0: shunt and bus, continuous
MAX_PLAUSIBLE_BUS_V = 6.5  # the capacitor bank is charged to about 5.2 V
CONFIG_CHECK_S = 10.0
SLOW_FAILURE_S = 0.05    # a failed read that took longer than this was a bus timeout


class _SmbusData(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16), ("block", ctypes.c_uint8 * 34)]


class _SmbusIoctl(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_uint8), ("command", ctypes.c_uint8),
                ("size", ctypes.c_uint32), ("data", ctypes.POINTER(_SmbusData))]


def decode_shunt_v(word):
    """Shunt register (already byte-swapped to host order) -> volts."""
    return (word - 0x10000 if word & 0x8000 else word) * SHUNT_LSB_V


def decode_bus_v(word):
    """Bus register (already byte-swapped to host order) -> volts."""
    return (word >> 3) * BUS_LSB_V


class Ina219:
    def __init__(self, bus=1, address=0x40, shunt_ohms=0.010, retry_s=1.0, monotonic=time.monotonic):
        self.dev = f"/dev/i2c-{bus}"
        self.address = address
        self.shunt_ohms = shunt_ohms
        self.retry_s = retry_s
        self.monotonic = monotonic
        self.fd = None
        self.config = None
        self._retry_at = 0.0
        self._config_at = 0.0
        self._last_error = ""

    def _open(self):
        fd = os.open(self.dev, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(fd, I2C_SLAVE, self.address)
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        self._config_at = 0.0

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _read_word(self, reg):
        """SMBus "read word data": one combined transaction, as smbus2's read_word_data()."""
        data = _SmbusData()
        msg = _SmbusIoctl(I2C_SMBUS_READ, reg, I2C_SMBUS_WORD_DATA, ctypes.pointer(data))
        fcntl.ioctl(self.fd, I2C_SMBUS, msg)
        w = data.word
        return ((w & 0xFF) << 8) | (w >> 8)   # the INA219 sends the high byte first

    def read(self):
        """Return (current_a, capacitor_v). Positive current = charging."""
        start = self.monotonic()
        if start < self._retry_at:
            raise ReadError(f"waiting to retry after: {self._last_error}")
        try:
            if self.fd is None:
                self._open()
            if start >= self._config_at:
                self.config = self._read_word(REG_CONFIG)
                self._config_at = start + CONFIG_CHECK_S
            if self.config & 0x7 != MODE_CONTINUOUS:
                self._config_at = 0.0
                raise ReadError(f"INA219 not converting continuously (config 0x{self.config:04X}): "
                                "readings would be stale")
            shunt_v = decode_shunt_v(self._read_word(REG_SHUNT))
            bus_v = decode_bus_v(self._read_word(REG_BUS))
            if bus_v > MAX_PLAUSIBLE_BUS_V:
                raise ReadError(f"implausible capacitor voltage {bus_v:.2f} V")
        except (OSError, ReadError) as e:
            self.close()
            self._last_error = f"{self.dev} 0x{self.address:02X}: {e}"
            # A quick failure (device did not answer) is retried at the next sample.
            # A slow one (bus timeout, about 1 s) would stall the loop: back off.
            if self.monotonic() - start > SLOW_FAILURE_S:
                self._retry_at = self.monotonic() + self.retry_s
            raise ReadError(self._last_error) from None
        return shunt_v / self.shunt_ohms, bus_v


# ------------------------------------------------------------------------ GPIO

# Linux GPIO v2 character-device uAPI (linux/gpio.h)
GPIO_GET_CHIPINFO_IOCTL = 0x8044B401
GPIO_V2_GET_LINE_IOCTL = 0xC250B407
GPIO_V2_LINE_GET_VALUES_IOCTL = 0xC010B40E
GPIO_V2_LINE_FLAG_INPUT = 1 << 2
GPIO_V2_LINE_FLAG_BIAS_DISABLED = 1 << 10


def find_gpiochip(label):
    """Return the /dev path of the GPIO chip whose label contains `label`."""
    try:
        names = sorted(n for n in os.listdir("/dev") if n.startswith("gpiochip"))
    except OSError as e:
        raise ReadError(f"cannot list /dev: {e}") from None
    for name in names:
        try:
            fd = os.open("/dev/" + name, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            info = bytearray(68)   # struct gpiochip_info: name[32], label[32], lines
            fcntl.ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, info)
            if label.encode() in bytes(info[32:64]):
                return "/dev/" + name
        except OSError:
            continue
        finally:
            os.close(fd)
    raise ReadError(f"no accessible GPIO chip labelled {label!r}")


class GpioInput:
    def __init__(self, line=26, chip_label="pinctrl-rp1", settle_s=0.1, consumer="radar-power",
                 retry_s=1.0, monotonic=time.monotonic, sleep=time.sleep):
        self.line = line
        self.chip_label = chip_label
        self.settle_s = settle_s
        self.consumer = consumer.encode()[:31]
        self.retry_s = retry_s
        self.monotonic = monotonic
        self.sleep = sleep
        self.fd = None
        self._retry_at = 0.0
        self._last_error = ""

    def _open(self):
        chip = find_gpiochip(self.chip_label)
        fd = os.open(chip, os.O_RDONLY | os.O_CLOEXEC)
        try:
            req = bytearray(592)   # struct gpio_v2_line_request
            struct.pack_into("I", req, 0, self.line)                 # offsets[0]
            struct.pack_into("32s", req, 256, self.consumer)         # consumer
            struct.pack_into("Q", req, 288,                          # config.flags
                             GPIO_V2_LINE_FLAG_INPUT | GPIO_V2_LINE_FLAG_BIAS_DISABLED)
            struct.pack_into("I", req, 560, 1)                       # num_lines
            fcntl.ioctl(fd, GPIO_V2_GET_LINE_IOCTL, req)
            self.fd = struct.unpack_from("i", req, 588)[0]
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise ReadError(f"GPIO {self.line} is in use by another process") from None
            raise
        finally:
            os.close(fd)
        # The line may have been pulled down until now; it needs time to rise.
        self.sleep(self.settle_s)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def read(self):
        """Return 1 (external power present) or 0."""
        if self.monotonic() < self._retry_at:
            raise ReadError(f"waiting to retry after: {self._last_error}")
        try:
            if self.fd is None:
                self._open()
            vals = bytearray(struct.pack("QQ", 0, 1))   # struct gpio_v2_line_values: bits, mask
            fcntl.ioctl(self.fd, GPIO_V2_LINE_GET_VALUES_IOCTL, vals)
            return struct.unpack("QQ", vals)[0] & 1
        except (OSError, ReadError) as e:
            self.close()
            self._last_error = f"GPIO {self.line}: {e}"
            self._retry_at = self.monotonic() + self.retry_s
            raise ReadError(self._last_error) from None
