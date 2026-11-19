// Stand-in SDR for the tests (tests/tools/). Linked only into sdr_gpio_safe_fake, which
// contains no UHD and cannot touch hardware. Behaviour comes from the environment variable
// SDR_GPIO_SAFE_FAKE, a comma-separated list of key=value:
//   found=N          number of SDRs found (default 1)
//   find_throws=1    looking for the SDR fails
//   open_throws=1    initialisation fails
//   hang=find|open   that step never returns
//   lost_writes=N    the first N OUT writes have no effect
//   write_throws=N   the first N writes fail
//   reverts=N        N times, the pin goes back to its start level after one good readback
//   stuck=1          the pin never changes
//   log=PATH         append every write and readback to this file
// The pin model follows UHD: after initialisation the pin is an output under ATR control
// and low, and an OUT write only takes effect on a pin that is already under manual control.
#include "gpio_port.hpp"

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <thread>

namespace {

std::map<std::string, std::string> settings() {
  std::map<std::string, std::string> out;
  const char* env = std::getenv("SDR_GPIO_SAFE_FAKE");
  std::stringstream ss(env ? env : "");
  std::string item;
  while (std::getline(ss, item, ',')) {
    const auto eq = item.find('=');
    if (eq != std::string::npos) out[item.substr(0, eq)] = item.substr(eq + 1);
  }
  return out;
}

int number(const std::map<std::string, std::string>& s, const std::string& key, int fallback) {
  const auto it = s.find(key);
  return it == s.end() ? fallback : std::atoi(it->second.c_str());
}

void hang_if(const std::map<std::string, std::string>& s, const std::string& step) {
  const auto it = s.find("hang");
  if (it != s.end() && it->second == step)
    for (;;) std::this_thread::sleep_for(std::chrono::seconds(60));
}

class FakePort : public GpioPort {
public:
  explicit FakePort(const std::map<std::string, std::string>& s)
      : lost_writes_(number(s, "lost_writes", 0)), write_throws_(number(s, "write_throws", 0)),
        reverts_(number(s, "reverts", 0)), stuck_(number(s, "stuck", 0) != 0),
        log_(s.count("log") ? s.at("log") : "") {}

  void write(const std::string& attr, uint32_t value, uint32_t mask) override {
    log(attr + " " + std::to_string(value) + " " + std::to_string(mask));
    if (write_throws_ > 0) {
      write_throws_--;
      throw std::runtime_error("fake: write failed");
    }
    if (attr == "CTRL") {
      manual_ = (manual_ & ~mask) | (~value & mask);  // CTRL bit 0 = manual
    } else if (attr == "DDR") {
      output_ = (output_ & ~mask) | (value & mask);
    } else if (attr == "OUT") {
      if (lost_writes_ > 0) {
        lost_writes_--;
        return;
      }
      const uint32_t applied = mask & manual_;
      out_ = (out_ & ~applied) | (value & applied);
      good_reads_ = 0;
    }
  }

  uint32_t readback() override {
    uint32_t level = stuck_ ? 0 : (out_ & manual_ & output_);
    if (level != 0 && reverts_ > 0 && good_reads_ >= 1) {
      reverts_--;
      out_ = 0;
      level = 0;
    }
    if (level != 0) good_reads_++;
    log("READBACK " + std::to_string(level));
    return level;
  }

private:
  void log(const std::string& line) {
    if (!log_.empty()) std::ofstream(log_, std::ios::app) << line << "\n";
  }
  int lost_writes_, write_throws_, reverts_;
  bool stuck_;
  std::string log_;
  uint32_t manual_ = 0, output_ = 0xffffffff, out_ = 0;
  int good_reads_ = 0;
};

}  // namespace

int find_sdr(const std::string&, std::string& description) {
  const auto s = settings();
  hang_if(s, "find");
  if (number(s, "find_throws", 0)) throw std::runtime_error("fake: cannot\nenumerate");
  description = "type=fake,serial=TEST";
  return number(s, "found", 1);
}

std::unique_ptr<GpioPort> open_sdr(const std::string&, const std::string&) {
  const auto s = settings();
  hang_if(s, "open");
  if (number(s, "open_throws", 0)) throw std::runtime_error("fake: init failed");
  return std::make_unique<FakePort>(s);
}
