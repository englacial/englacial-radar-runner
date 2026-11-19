// Stand-in SDR for the tests (tests/tools/). Linked only into sdr_gpio_safe_fake, which
// contains no UHD and cannot touch hardware. Behaviour comes from the environment variable
// SDR_GPIO_SAFE_FAKE, a comma-separated list of key=value:
//   found=N              number of SDRs found (default 1)
//   find_throws=1        looking for the SDR fails
//   sysfs=DIR            directory to search for USB devices (default: none connected)
//   open_throws=1        initialisation fails
//   hang=find|open|write|read|close   that step never returns
//   lost=ATTR:N          the first N writes of CTRL, DDR or OUT have no effect
//   write_throws=N       the first N writes fail
//   read_throws=N        the first N readbacks fail
//   reverts=N            N times, the pin goes back to low after one good readback
//   stuck=1              OUT never takes effect
//   log=PATH             append every write and readback to this file
// The pin model follows UHD and the FPGA (gpio_atr_3000.cpp, gpio_atr.v): after
// initialisation every pin is an output under ATR control at level 0; an OUT write only
// takes effect on a pin that is under manual control at that time; the readback is the
// commanded level for an output and the pulled-up pin (1) for an input.
#include "gpio_port.hpp"

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <thread>

namespace {

typedef std::map<std::string, std::string> Settings;

Settings settings() {
  Settings out;
  const char* env = std::getenv("SDR_GPIO_SAFE_FAKE");
  std::stringstream ss(env ? env : "");
  std::string item;
  while (std::getline(ss, item, ',')) {
    const auto eq = item.find('=');
    if (eq != std::string::npos) out[item.substr(0, eq)] = item.substr(eq + 1);
  }
  return out;
}

std::string text(const Settings& s, const std::string& key) {
  const auto it = s.find(key);
  return it == s.end() ? "" : it->second;
}

int number(const Settings& s, const std::string& key, int fallback) {
  return s.count(key) ? std::atoi(s.at(key).c_str()) : fallback;
}

void hang_if(const Settings& s, const std::string& step) {
  if (text(s, "hang") == step)
    for (;;) std::this_thread::sleep_for(std::chrono::seconds(60));
}

class FakePort : public GpioPort {
public:
  explicit FakePort(const Settings& s)
      : s_(s), write_throws_(number(s, "write_throws", 0)), read_throws_(number(s, "read_throws", 0)),
        reverts_(number(s, "reverts", 0)), stuck_(number(s, "stuck", 0) != 0), log_(text(s, "log")) {
    const std::string lost = text(s, "lost");
    const auto colon = lost.find(':');
    if (colon != std::string::npos) {
      lost_attr_ = lost.substr(0, colon);
      lost_ = std::atoi(lost.c_str() + colon + 1);
    }
  }
  ~FakePort() override { hang_if(s_, "close"); }

  void write(const std::string& attr, uint32_t value, uint32_t mask) override {
    log(attr + " " + std::to_string(value) + " " + std::to_string(mask));
    hang_if(s_, "write");
    if (write_throws_ > 0) {
      write_throws_--;
      throw std::runtime_error("fake: write failed");
    }
    if (attr == lost_attr_ && lost_ > 0) {
      lost_--;
      return;
    }
    if (attr == "CTRL") {
      manual_ = (manual_ & ~mask) | (~value & mask);  // CTRL bit 0 = manual
    } else if (attr == "DDR") {
      output_ = (output_ & ~mask) | (value & mask);
    } else if (attr == "OUT" && !stuck_) {
      const uint32_t applied = mask & manual_;
      out_ = (out_ & ~applied) | (value & applied);
      good_reads_ = 0;
    }
  }

  uint32_t readback() override {
    hang_if(s_, "read");
    if (read_throws_ > 0) {
      read_throws_--;
      log("READBACK failed");
      throw std::runtime_error("fake: readback failed");
    }
    if ((out_ & 1) && reverts_ > 0 && good_reads_ >= 1) {
      reverts_--;
      out_ &= ~uint32_t(1);
    }
    // outputs: the commanded level (0 under ATR, OUT under manual control); inputs: pulled up
    const uint32_t level = ((output_ & manual_ & out_) | ~output_) & 0xff;
    if (level & 1) good_reads_++;
    log("READBACK " + std::to_string(level));
    return level;
  }

private:
  void log(const std::string& line) {
    if (!log_.empty()) std::ofstream(log_, std::ios::app) << line << "\n";
  }
  const Settings s_;
  int write_throws_, read_throws_, reverts_;
  bool stuck_;
  std::string log_, lost_attr_;
  int lost_ = 0;
  uint32_t manual_ = 0, output_ = 0xffffffff, out_ = 0;
  int good_reads_ = 0;
};

}  // namespace

int find_sdr(const std::string&, std::string& description) {
  const Settings s = settings();
  hang_if(s, "find");
  if (number(s, "find_throws", 0)) throw std::runtime_error("fake: cannot\nenumerate");
  description = "type=fake,serial=TEST";
  return number(s, "found", 1);
}

bool sdr_on_usb(std::string& where) {
  const std::string dir = text(settings(), "sysfs");
  return !dir.empty() && usb_vendor_present(dir, "2500", where);
}

std::unique_ptr<GpioPort> open_sdr(const std::string&) {
  const Settings s = settings();
  hang_if(s, "open");
  if (number(s, "open_throws", 0)) throw std::runtime_error("fake: init failed");
  return std::make_unique<FakePort>(s);
}
