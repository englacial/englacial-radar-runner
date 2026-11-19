// The only access sdr_gpio_safe has to the SDR. uhd_port.cpp implements it with
// UHD; fake_port.cpp is a stand-in for the tests and never touches hardware.
#pragma once
#include <cstdint>
#include <memory>
#include <string>

class GpioPort {
public:
  virtual ~GpioPort() = default;
  // Set the bits of `mask` in a GPIO attribute ("CTRL", "DDR", "OUT") to `value`.
  virtual void write(const std::string& attr, uint32_t value, uint32_t mask) = 0;
  // Level of every pin of the bank as the SDR reads it.
  virtual uint32_t readback() = 0;
};

// Number of SDRs matching the device arguments; `description` describes the first.
// Loads the SDR's firmware if it has none yet. Does not initialise the SDR.
int find_sdr(const std::string& args, std::string& description);

// Initialise the SDR and return its GPIO bank. Throws std::exception on failure.
std::unique_ptr<GpioPort> open_sdr(const std::string& args, const std::string& bank);
