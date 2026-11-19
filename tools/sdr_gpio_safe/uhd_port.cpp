// GpioPort on a real SDR through UHD. find_sdr and open_sdr talk to the device.
#include "gpio_port.hpp"

#include <uhd/device.hpp>
#include <uhd/usrp/multi_usrp.hpp>

namespace {

class UhdPort : public GpioPort {
public:
  explicit UhdPort(uhd::usrp::multi_usrp::sptr usrp) : usrp_(usrp) {}
  void write(const std::string& attr, uint32_t value, uint32_t mask) override {
    usrp_->set_gpio_attr(AMP_BANK, attr, value, mask);
  }
  uint32_t readback() override { return usrp_->get_gpio_attr(AMP_BANK, "READBACK"); }

private:
  uhd::usrp::multi_usrp::sptr usrp_;
};

}  // namespace

int find_sdr(const std::string& args, std::string& description) {
  const uhd::device_addrs_t found = uhd::device::find(uhd::device_addr_t(args), uhd::device::USRP);
  if (!found.empty()) description = found[0].to_string();
  return static_cast<int>(found.size());
}

bool sdr_on_usb(std::string& where) {
  return usb_vendor_present("/sys/bus/usb/devices", "2500", where);  // 2500 = Ettus Research
}

std::unique_ptr<GpioPort> open_sdr(const std::string& args) {
  return std::make_unique<UhdPort>(uhd::usrp::multi_usrp::make(uhd::device_addr_t(args)));
}
