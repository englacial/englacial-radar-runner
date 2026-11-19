// sdr_gpio_safe: put the SDR pin that enables the power amplifier into the
// amplifier-shutdown state and verify it. That is its only action. See README.md.
// Ported from ~/radar-testbench/pa_bringup/gpio_hold.cpp (its --high mode).
//
// What the pin does, and why the writes are ordered, repeated and re-checked
// (bench notes: ~/radar-testbench/pa_bringup/STATUS.md; UHD: gpio_atr_3000.cpp, b200_impl.cpp):
// - Every UHD initialisation makes all pins of the bank outputs under automatic (ATR)
//   control with all levels 0. The pin is therefore low, which enables the amplifier on
//   this unit, from inside the initialisation until the first write here takes effect
//   (0.3-0.5 s measured). Nothing in this program can avoid that.
// - Until then the readback reads 0. The bench notes call the readback unreliable right
//   after initialisation; so it is only ever trusted after a write made by this program.
// - UHD sends an OUT level to the SDR only for a pin that is under manual control at the
//   time of that write, so OUT has to be written after CTRL. A level written before is
//   not applied, which fits the bench note that the first writes may not stick. All three
//   writes are repeated until the readback agrees, up to WRITES_PER_ROUND times.
// - On the bench a level written very early was seen undone shortly afterwards (cause not
//   found). So the pin is read again after a settle time with no write in between, and
//   only that second reading counts as verified. If it disagrees the writes start again,
//   up to MAX_ROUNDS times.
//
// Time limit: a UHD call into a hung USB device may never return and cannot be cancelled.
// A second thread therefore ends the whole process when the time is up. If the stuck call
// is inside the kernel and cannot be interrupted, the process outlives even that (and
// SIGKILL), which is why the amp_safe wrapper stops waiting for it.
#include "gpio_port.hpp"

#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

// Exit codes (also in README.md and in amp_safe)
enum {
  EXIT_SAFE = 0,        // pin verified in the amplifier-shutdown state
  EXIT_NOT_FOUND = 2,   // no SDR found; nothing written
  EXIT_UNVERIFIED = 3,  // SDR found, but the state could not be verified
  EXIT_TIMED_OUT = 4,   // no result within the time limit; pin state unknown
  EXIT_BAD_USAGE = 64,  // bad command line; SDR not touched
};

// Defaults: the bench unit (B206mini, Empower 1094 control board on GPIO_0, high = shutdown)
static const char* const DEFAULT_ARGS = "";
static const char* const DEFAULT_BANK = "FP0";
static const int DEFAULT_PIN = 0;
static const bool DEFAULT_SHUTDOWN_HIGH = true;
static const int DEFAULT_SETTLE_MS = 1500;
static const double DEFAULT_TIMEOUT_S = 20.0;

static const int WRITES_PER_ROUND = 30;  // 30 x (50 + 50 ms) = 3 s
static const int MAX_ROUNDS = 3;
static const int READ_DELAY_MS = 50;     // from a write to its readback
static const int RETRY_DELAY_MS = 50;

struct Options {
  std::string args = DEFAULT_ARGS;
  std::string bank = DEFAULT_BANK;
  int pin = DEFAULT_PIN;
  bool shutdown_high = DEFAULT_SHUTDOWN_HIGH;
  int settle_ms = DEFAULT_SETTLE_MS;
  double timeout_s = DEFAULT_TIMEOUT_S;
};

struct Result {
  int code;
  std::string text;
};

typedef std::chrono::steady_clock Clock;
static const Clock::time_point g_start = Clock::now();
static std::atomic<const char*> g_stage{"starting"};
static std::mutex g_result_mutex;
static int g_exit_code = -1;  // -1 until the result line has been printed

static double elapsed_s() { return std::chrono::duration<double>(Clock::now() - g_start).count(); }
static void sleep_ms(int ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }

static std::string fmt(const char* format, double value) {
  char buf[64];
  std::snprintf(buf, sizeof buf, format, value);
  return buf;
}

// UHD error texts span several lines; the result is one line.
static std::string one_line(std::string s) {
  for (char& c : s)
    if (c == '\n' || c == '\r' || c == '\t') c = ' ';
  if (s.size() > 200) s = s.substr(0, 200) + "...";
  return s;
}

// Print the one result line, unless one has been printed already. Returns the exit code that stands.
// write(2) directly: no stream buffers or locks that a stuck thread could be holding.
static int report(int code, const std::string& text) {
  std::lock_guard<std::mutex> lock(g_result_mutex);
  if (g_exit_code < 0) {
    const std::string line = "sdr_gpio_safe: " + text + "\n";
    size_t done = 0;
    while (done < line.size()) {
      const ssize_t n = ::write(STDOUT_FILENO, line.data() + done, line.size() - done);
      if (n <= 0) break;
      done += static_cast<size_t>(n);
    }
    g_exit_code = code;
  }
  return g_exit_code;
}

static void time_limit(double timeout_s) {
  std::this_thread::sleep_until(g_start + std::chrono::duration_cast<Clock::duration>(
                                              std::chrono::duration<double>(timeout_s)));
  const int code = report(EXIT_TIMED_OUT, "TIMEOUT: no result within " + fmt("%.1f", timeout_s)
                                              + " s, stuck while " + g_stage.load()
                                              + "; pin state unknown");
  _exit(code);  // without unwinding: the main thread may be stuck inside UHD
}

static bool reads_shutdown(GpioPort& port, uint32_t mask, bool shutdown_high) {
  return ((port.readback() & mask) != 0) == shutdown_high;
}

// Find and open the SDR, then write and verify the pin. `port` is left open for the caller to close.
static Result make_safe(const Options& opt, std::unique_ptr<GpioPort>& port) {
  const uint32_t mask = uint32_t(1) << opt.pin;
  const std::string pin = opt.bank + " pin " + std::to_string(opt.pin);
  const std::string want = opt.shutdown_high ? "high" : "low";
  const std::string wrong = opt.shutdown_high ? "low" : "high";

  g_stage = "looking for the SDR";
  std::string sdr;
  int found = 0;
  try {
    found = find_sdr(opt.args, sdr);
  } catch (const std::exception& e) {
    return {EXIT_NOT_FOUND, "NOT_FOUND: looking for the SDR failed: " + one_line(e.what()) + "; pin not written"};
  } catch (...) {
    return {EXIT_NOT_FOUND, "NOT_FOUND: looking for the SDR failed; pin not written"};
  }
  if (found == 0)
    return {EXIT_NOT_FOUND, "NOT_FOUND: no SDR for device arguments \"" + opt.args
                                + "\" (absent, unpowered, or in use by another process); pin not written"};
  if (found > 1)
    return {EXIT_UNVERIFIED, "UNVERIFIED: " + std::to_string(found) + " SDRs match device arguments \""
                                 + opt.args + "\"; none opened, pin not written; select one with --args"};

  g_stage = "initialising the SDR";
  const double init_start = elapsed_s();
  std::string error;
  try {
    port = open_sdr(opt.args, opt.bank);
  } catch (const std::exception& e) {
    error = one_line(e.what());
  } catch (...) {
    error = "unknown error";
  }
  if (!port)
    return {EXIT_UNVERIFIED, "UNVERIFIED: SDR found (" + sdr + ") but not initialised: " + error
                                 + "; pin state unknown"};
  const double init_s = elapsed_s() - init_start;

  int writes = 0;
  int rounds = 0;
  std::string detail = pin + " never read " + want;
  while (rounds < MAX_ROUNDS) {
    rounds++;
    g_stage = "writing the pin";
    bool ok = false;
    for (int i = 0; i < WRITES_PER_ROUND && !ok; i++) {
      if (i > 0) sleep_ms(RETRY_DELAY_MS);
      try {
        port->write("CTRL", 0, mask);                             // manual control, not ATR
        port->write("DDR", mask, mask);                           // output
        port->write("OUT", opt.shutdown_high ? mask : 0, mask);   // the shutdown level; after CTRL
        writes++;
        sleep_ms(READ_DELAY_MS);
        ok = reads_shutdown(*port, mask, opt.shutdown_high);
      } catch (const std::exception& e) {
        error = one_line(e.what());
      }
    }
    if (!ok) break;
    detail = pin + " read " + want + " once but could not be read again";

    g_stage = "waiting to read the pin again";
    sleep_ms(opt.settle_ms);
    try {
      if (reads_shutdown(*port, mask, opt.shutdown_high))
        return {EXIT_SAFE, "OK: " + pin + " reads " + want + " (amplifier shutdown) as a manual output, and again "
                               + std::to_string(opt.settle_ms) + " ms later; " + std::to_string(writes)
                               + (writes == 1 ? " write, " : " writes, ") + "SDR initialised in "
                               + fmt("%.2f", init_s) + " s, total " + fmt("%.2f", elapsed_s()) + " s; " + sdr};
      detail = pin + " went back to " + wrong + " after reading " + want;
    } catch (const std::exception& e) {
      error = one_line(e.what());
    }
  }
  return {EXIT_UNVERIFIED, "UNVERIFIED: " + detail + " (" + std::to_string(writes) + " writes, "
                               + std::to_string(rounds) + (rounds == 1 ? " round" : " rounds") + ")"
                               + (error.empty() ? "" : "; last error: " + error)
                               + "; the amplifier may be enabled; " + sdr};
}

static void print_help() {
  std::printf(
      "Usage: sdr_gpio_safe [options]\n"
      "\n"
      "Opens the SDR, drives the amplifier-enable pin to the level that shuts the\n"
      "amplifier down, as a manually controlled output, and verifies it by reading the\n"
      "pin back. It has no other action. Opening the SDR enables the amplifier for\n"
      "about half a second before the pin is written: see README.md.\n"
      "\n"
      "Options (defaults are the bench unit):\n"
      "  --args STRING           UHD device arguments (default \"%s\")\n"
      "  --bank NAME             GPIO bank (default %s)\n"
      "  --pin N                 bit of the bank, 0-31 (default %d = GPIO_0; ORCA's\n"
      "                          pwr_amp_pin minus 2)\n"
      "  --shutdown-level LEVEL  high or low: the pin level at which the amplifier is\n"
      "                          shut down (default %s)\n"
      "  --settle-ms N           wait before the pin is read the second time, 0-10000\n"
      "                          (default %d)\n"
      "  --timeout SECONDS       give up after this long, 1-300 (default %.0f)\n"
      "  -h, --help              show this text; the SDR is not touched\n"
      "\n"
      "Prints one line to standard output; UHD's own messages go to standard error.\n"
      "Exit codes:\n"
      "   %d  OK          pin verified in the amplifier-shutdown state\n"
      "   %d  NOT_FOUND   no SDR found; nothing written\n"
      "   %d  UNVERIFIED  SDR found, but the state could not be verified\n"
      "   %d  TIMEOUT     no result within the time limit; pin state unknown\n"
      "  %d  USAGE       bad command line; SDR not touched\n",
      DEFAULT_ARGS, DEFAULT_BANK, DEFAULT_PIN, DEFAULT_SHUTDOWN_HIGH ? "high" : "low", DEFAULT_SETTLE_MS,
      DEFAULT_TIMEOUT_S, EXIT_SAFE, EXIT_NOT_FOUND, EXIT_UNVERIFIED, EXIT_TIMED_OUT, EXIT_BAD_USAGE);
}

static bool parse_number(const char* text, double low, double high, bool integer, double& value) {
  char* end = nullptr;
  value = std::strtod(text, &end);
  if (end == text || *end != '\0' || !std::isfinite(value)) return false;
  if (std::strspn(text, "0123456789.") != std::strlen(text)) return false;  // plain decimal only
  if (integer && value != std::floor(value)) return false;
  return value >= low && value <= high;
}

enum Parsed { PARSED_RUN, PARSED_HELP, PARSED_ERROR };

// Everything on the command line is checked here, before anything can touch the SDR.
static Parsed parse_args(int argc, char* argv[], Options& opt, std::string& error) {
  for (int i = 1; i < argc; i++)
    if (!std::strcmp(argv[i], "-h") || !std::strcmp(argv[i], "--help")) return PARSED_HELP;
  for (int i = 1; i < argc; i += 2) {
    const std::string name = argv[i];
    if (name != "--args" && name != "--bank" && name != "--pin" && name != "--shutdown-level"
        && name != "--settle-ms" && name != "--timeout") {
      error = "unknown argument " + name;
      return PARSED_ERROR;
    }
    if (i + 1 >= argc) {
      error = name + " needs a value";
      return PARSED_ERROR;
    }
    const char* text = argv[i + 1];
    double number = 0;
    bool ok = true;
    if (name == "--args") {
      opt.args = text;
    } else if (name == "--bank") {
      opt.bank = text;
      ok = !opt.bank.empty();
    } else if (name == "--pin") {
      ok = parse_number(text, 0, 31, true, number);
      opt.pin = static_cast<int>(number);
    } else if (name == "--shutdown-level") {
      ok = !std::strcmp(text, "high") || !std::strcmp(text, "low");
      opt.shutdown_high = !std::strcmp(text, "high");
    } else if (name == "--settle-ms") {
      ok = parse_number(text, 0, 10000, true, number);
      opt.settle_ms = static_cast<int>(number);
    } else {
      ok = parse_number(text, 1, 300, false, number);
      opt.timeout_s = number;
    }
    if (!ok) {
      error = "bad value for " + name + ": \"" + text + "\"";
      return PARSED_ERROR;
    }
  }
  return PARSED_RUN;
}

int main(int argc, char* argv[]) {
  Options opt;
  std::string error;
  const Parsed parsed = parse_args(argc, argv, opt, error);
  if (parsed == PARSED_HELP) {
    print_help();
    return EXIT_SAFE;
  }
  if (parsed == PARSED_ERROR) return report(EXIT_BAD_USAGE, "USAGE: " + error + " (see --help); SDR not touched");

  try {
    std::thread(time_limit, opt.timeout_s).detach();
  } catch (const std::exception&) {
    return report(EXIT_UNVERIFIED, "UNVERIFIED: cannot start the time-limit thread; SDR not touched");
  }

  std::unique_ptr<GpioPort> port;
  const Result result = make_safe(opt, port);
  const int code = report(result.code, result.text);
  g_stage = "closing the SDR";  // still under the time limit; the result above stands
  port.reset();
  return code;
}
