// The only access sdr_gpio_safe has to the SDR. uhd_port.cpp implements it with
// UHD; fake_port.cpp is a stand-in for the tests and never touches hardware.
#pragma once
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>

// The amplifier-enable pin of this radar: front-panel bank FP0, bit 0 (GPIO_0).
// High = amplifier shutdown. Fixed on purpose: see README.md.
constexpr const char* AMP_BANK = "FP0";
constexpr uint32_t AMP_PIN_MASK = 0x1;

class GpioPort {
public:
  virtual ~GpioPort() = default;
  // Set the bits of `mask` in an attribute of AMP_BANK ("CTRL", "DDR", "OUT") to `value`.
  virtual void write(const std::string& attr, uint32_t value, uint32_t mask) = 0;
  // The bank as the SDR reports it: for an output the level the FPGA is commanding,
  // for an input the level at the pin.
  virtual uint32_t readback() = 0;
};

// Number of SDRs matching the device arguments; `description` describes the first.
// Loads the SDR's firmware if it has none yet. Does not initialise the SDR.
int find_sdr(const std::string& args, std::string& description);

// True if a USB device of the SDR's maker is connected, whether or not UHD can use it
// (UHD does not list a device that another process has open). Reads sysfs only.
bool sdr_on_usb(std::string& where);

// Initialise the SDR and return its AMP_BANK. Throws std::exception on failure.
// From somewhere inside this call until the caller's writes the amplifier is enabled.
std::unique_ptr<GpioPort> open_sdr(const std::string& args);

// Is a USB device with this vendor ID (four hex digits, lower case) listed in `sysfs_dir`?
inline bool usb_vendor_present(const std::string& sysfs_dir, const std::string& vendor, std::string& where) {
  std::error_code ec;
  for (std::filesystem::directory_iterator it(sysfs_dir, ec), end; !ec && it != end; it.increment(ec)) {
    std::ifstream file(it->path() / "idVendor");
    std::string id;
    if (file >> id && id == vendor) {
      where = "USB " + it->path().filename().string();
      return true;
    }
  }
  return false;
}
