"""Tests of the hardware readers that need no hardware: decoding and error handling.

The I2C and GPIO system calls are replaced by fakes; no device is opened.
"""
import errno
import struct
import unittest
from unittest import mock

from power import hardware as H


class DecodeTest(unittest.TestCase):
    def test_register_values_from_the_bring_up_log(self):
        # UPS_BRINGUP.md step 3: shunt 0x0040 = +0.64 mV, bus 0x286A = 5.172 V
        self.assertAlmostEqual(H.decode_shunt_v(0x0040), 0.00064)
        self.assertAlmostEqual(H.decode_bus_v(0x286A), 5.172)

    def test_negative_shunt_is_twos_complement(self):
        self.assertAlmostEqual(H.decode_shunt_v(0xFFFF), -10e-6)       # -1 LSB, no off-by-one
        self.assertAlmostEqual(H.decode_shunt_v(0xFC32), -0.00974)     # -9.74 mV = -0.974 A
        self.assertAlmostEqual(H.decode_shunt_v(0x8000), -0.32768)
        self.assertAlmostEqual(H.decode_shunt_v(0x7FFF), 0.32767)

    def test_bus_flag_bits_are_ignored(self):
        self.assertAlmostEqual(H.decode_bus_v(0x286A | 0x3), H.decode_bus_v(0x2868))


class FakeBus:
    """Stands in for os.open/os.close/fcntl.ioctl on the I2C device."""

    def __init__(self, regs):
        self.regs = dict(regs)       # register -> value as the INA219 holds it
        self.fail = None             # an OSError to raise from every transfer
        self.slow = 0.0              # simulated duration of a failing transfer
        self.now = 0.0
        self.opened = 0
        self.closed = 0
        self.transfers = []

    def open(self, path, flags):
        self.opened += 1
        return 99

    def close(self, fd):
        self.closed += 1

    def ioctl(self, fd, request, arg):
        if request == H.I2C_SLAVE:
            self.address = arg
            return 0
        assert request == H.I2C_SMBUS and arg.read_write == H.I2C_SMBUS_READ
        assert arg.size == H.I2C_SMBUS_WORD_DATA
        self.transfers.append(arg.command)
        if self.fail:
            self.now += self.slow
            raise self.fail
        value = self.regs[arg.command]
        arg.data.contents.word = ((value & 0xFF) << 8) | (value >> 8)   # low byte first on the wire
        return 0


class Ina219Test(unittest.TestCase):
    def make(self, regs=None):
        bus = FakeBus(regs or {0: 0x399F, 1: 0x0002, 2: 0x286A})
        patches = [mock.patch.object(H.os, "open", bus.open), mock.patch.object(H.os, "close", bus.close),
                   mock.patch.object(H.fcntl, "ioctl", bus.ioctl)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return H.Ina219(monotonic=lambda: bus.now), bus

    def test_reads_current_and_voltage(self):
        ina, bus = self.make()
        current, volts = ina.read()
        self.assertAlmostEqual(current, 0.002)          # 20 uV across 10 mOhm
        self.assertAlmostEqual(volts, 5.172)
        self.assertEqual(bus.address, 0x40)
        self.assertEqual(bus.transfers, [0, 1, 2])      # config is checked on the first read
        ina.read()
        self.assertEqual(bus.transfers, [0, 1, 2, 1, 2])
        self.assertEqual(bus.opened, 1)

    def test_discharge_current_is_negative(self):
        ina, bus = self.make({0: 0x399F, 1: 0xFC32, 2: 0x2850})
        self.assertAlmostEqual(ina.read()[0], -0.974)

    def test_only_reads_are_ever_issued(self):
        ina, bus = self.make()
        for _ in range(100):
            bus.now += 0.2
            ina.read()
        self.assertEqual(set(bus.transfers), {0, 1, 2})   # FakeBus asserts every transfer is a read

    def test_config_is_rechecked_periodically(self):
        ina, bus = self.make()
        for k in range(103):
            bus.now = round(k * 0.2, 6)
            ina.read()
        self.assertEqual(bus.transfers.count(0), 3)       # at 0, 10 and 20 s

    def test_quick_failure_is_retried_at_the_next_sample(self):
        ina, bus = self.make()
        bus.fail = OSError(errno.EREMOTEIO, "Remote I/O error")
        for _ in range(3):
            with self.assertRaises(H.ReadError) as cm:
                ina.read()
            self.assertIn("Remote I/O error", str(cm.exception))
        self.assertEqual((bus.opened, bus.closed), (3, 3))     # closed after each failure
        bus.fail = None
        self.assertAlmostEqual(ina.read()[1], 5.172)

    def test_bus_timeout_backs_off(self):
        ina, bus = self.make()
        bus.fail, bus.slow = OSError(errno.ETIMEDOUT, "timed out"), 1.0
        with self.assertRaises(H.ReadError):
            ina.read()
        n = len(bus.transfers)
        bus.now += 0.5
        with self.assertRaises(H.ReadError) as cm:      # still backing off: bus not touched
            ina.read()
        self.assertIn("waiting to retry", str(cm.exception))
        self.assertEqual(len(bus.transfers), n)
        bus.now += 0.6
        bus.fail = None
        self.assertAlmostEqual(ina.read()[1], 5.172)

    def test_powered_down_ina219_is_unreadable_not_stale(self):
        ina, bus = self.make({0: 0x3998, 1: 0x0002, 2: 0x286A})   # mode bits 000 = power-down
        with self.assertRaises(H.ReadError) as cm:
            ina.read()
        self.assertIn("not converting", str(cm.exception))
        bus.regs[0] = 0x399F
        ina.read()

    def test_implausible_voltage_is_rejected(self):
        ina, bus = self.make({0: 0x399F, 1: 0xFFFF, 2: 0xFFFF})   # bus stuck high: all ones
        with self.assertRaises(H.ReadError):
            ina.read()

    def test_missing_device_node(self):
        ina = H.Ina219(bus=63)
        with mock.patch.object(H.os, "open", side_effect=FileNotFoundError(2, "No such file")):
            with self.assertRaises(H.ReadError):
                ina.read()
        ina.close()


class FakeGpio:
    def __init__(self, labels=("pinctrl-bcm2712", "pinctrl-rp1"), value=1):
        self.labels = labels
        self.value = value
        self.fail_request = None
        self.fail_read = None
        self.request = None
        self.fds = {}
        self.next_fd = 10

    def listdir(self, path):
        return ["null", "tty"] + [f"gpiochip{i}" for i in range(len(self.labels))][::-1]

    def open(self, path, flags):
        self.next_fd += 1
        self.fds[self.next_fd] = path
        return self.next_fd

    def close(self, fd):
        self.fds.pop(fd, None)

    def ioctl(self, fd, request, buf):
        if request == H.GPIO_GET_CHIPINFO_IOCTL:
            index = int(self.fds[fd].removeprefix("/dev/gpiochip"))
            struct.pack_into("32s32sI", buf, 0, b"gpiochipN", self.labels[index].encode(), 54)
        elif request == H.GPIO_V2_GET_LINE_IOCTL:
            if self.fail_request:
                raise self.fail_request
            self.request = (self.fds[fd], struct.unpack_from("I", buf, 0)[0],
                            struct.unpack_from("Q", buf, 288)[0], struct.unpack_from("I", buf, 560)[0],
                            bytes(buf[256:288]).rstrip(b"\0"))
            struct.pack_into("i", buf, 588, 77)
        elif request == H.GPIO_V2_LINE_GET_VALUES_IOCTL:
            assert fd == 77
            if self.fail_read:
                raise self.fail_read
            struct.pack_into("Q", buf, 0, self.value)
        return 0


class GpioTest(unittest.TestCase):
    def make(self, **kw):
        g = FakeGpio(**kw)
        self.now = 0.0
        self.slept = []
        patches = [mock.patch.object(H.os, "listdir", g.listdir), mock.patch.object(H.os, "open", g.open),
                   mock.patch.object(H.os, "close", g.close), mock.patch.object(H.fcntl, "ioctl", g.ioctl)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return H.GpioInput(monotonic=lambda: self.now, sleep=self.slept.append), g

    def test_requests_line_26_on_the_rp1_chip_as_input_without_bias(self):
        gpio, g = self.make()
        self.assertEqual(gpio.read(), 1)
        chip, line, flags, num_lines, consumer = g.request
        self.assertEqual(chip, "/dev/gpiochip1")        # found by its label, not by its number
        self.assertEqual((line, num_lines), (26, 1))
        self.assertEqual(flags, H.GPIO_V2_LINE_FLAG_INPUT | H.GPIO_V2_LINE_FLAG_BIAS_DISABLED)
        self.assertEqual(consumer, b"radar-power")
        self.assertEqual(self.slept, [0.1])             # settles once, after the request
        g.value = 0
        self.assertEqual(gpio.read(), 0)
        self.assertEqual(self.slept, [0.1])
        self.assertEqual(set(g.fds), set())             # chip fds are closed again

    def test_chip_numbering_does_not_matter(self):
        gpio, g = self.make(labels=("pinctrl-rp1", "x", "y"))
        gpio.read()
        self.assertEqual(g.request[0], "/dev/gpiochip0")

    def test_missing_chip_is_a_read_error(self):
        gpio, g = self.make(labels=("a", "b"))
        with self.assertRaises(H.ReadError) as cm:
            gpio.read()
        self.assertIn("pinctrl-rp1", str(cm.exception))

    def test_line_in_use_is_a_read_error_and_is_retried_after_a_pause(self):
        gpio, g = self.make()
        g.fail_request = OSError(errno.EBUSY, "Device or resource busy")
        with self.assertRaises(H.ReadError) as cm:
            gpio.read()
        self.assertIn("in use", str(cm.exception))
        g.fail_request = None
        with self.assertRaises(H.ReadError):            # within the retry pause
            gpio.read()
        self.now = 1.1
        self.assertEqual(gpio.read(), 1)

    def test_read_failure_reopens_the_line(self):
        gpio, g = self.make()
        gpio.read()
        g.fail_read = OSError(errno.EIO, "I/O error")
        with self.assertRaises(H.ReadError):
            gpio.read()
        g.fail_read = None
        self.now = 2.0
        self.assertEqual(gpio.read(), 1)
        self.assertEqual(self.slept, [0.1, 0.1])        # requested, and settled, a second time


if __name__ == "__main__":
    unittest.main()
