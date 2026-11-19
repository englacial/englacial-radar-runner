// GpioPort on a real SDR through UHD. Everything in this file talks to the device.
#include "gpio_port.hpp"

#include <uhd/device.hpp>
#include <uhd/usrp/multi_usrp.hpp>

#include <algorithm>
#include <stdexcept>

namespace {

class UhdPort : public GpioPort {
public:
  UhdPort(uhd::usrp::multi_usrp::sptr usrp, const std::string& bank) : usrp_(usrp), bank_(bank) {}
  void write(const std::string& attr, uint32_t value, uint32_t mask) override {
    usrp_->set_gpio_attr(bank_, attr, value, mask);
  }
  uint32_t readback() override { return usrp_->get_gpio_attr(bank_, "READBACK"); }

private:
  uhd::usrp::multi_usrp::sptr usrp_;
  std::string bank_;
};

}  // namespace

int find_sdr(const std::string& args, std::string& description) {
  const uhd::device_addrs_t found = uhd::device::find(uhd::device_addr_t(args), uhd::device::USRP);
  if (!found.empty()) description = found[0].to_string();
  return static_cast<int>(found.size());
}

std::unique_ptr<GpioPort> open_sdr(const std::string& args, const std::string& bank) {
  // The amplifier is enabled from somewhere inside this call until the caller's first write.
  auto usrp = uhd::usrp::multi_usrp::make(uhd::device_addr_t(args));
  const auto banks = usrp->get_gpio_banks(0);
  if (std::find(banks.begin(), banks.end(), bank) == banks.end())
    throw std::runtime_error("the SDR has no GPIO bank " + bank);
  return std::make_unique<UhdPort>(usrp, bank);
}
