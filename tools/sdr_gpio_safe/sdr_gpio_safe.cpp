// sdr_gpio_safe: put the SDR pin that enables the power amplifier (FP0 GPIO_0) into the
// amplifier-shutdown state (high) and verify it. That is its only action. See README.md.
// Ported from ~/radar-testbench/pa_bringup/gpio_hold.cpp (its --high mode).
//
// What the pin does, and why the writes are ordered, repeated and re-checked
// (bench notes: ~/radar-testbench/pa_bringup/STATUS.md; UHD: gpio_atr_3000.cpp,
// b200_impl.cpp; FPGA: gpio_atr.v):
// - Every UHD initialisation makes all pins of the bank outputs under automatic (ATR)
//   control with all levels 0, and the SDR keeps its registers when a process ends. So
//   from inside the initialisation the pin is low, which enables the amplifier, and it
//   stays low until a write here takes effect (0.3-0.5 s on the bench) or for good if
//   this program ends before that. Hence the two kinds of failure below: before the
//   initialisation started the pin has not been changed by this run; after it started
//   the amplifier must be assumed enabled by this run until the pin is verified high.
// - The readback of an output is the level the FPGA is commanding, not the level at the
//   pin. It truthfully reads 0 after the initialisation. The bench notes call it
//   unreliable there; it is only trusted after a write made by this program.
// - UHD sends an OUT level to the SDR only for a pin that is under manual control at the
//   time of that write, so OUT has to be written after CTRL. A level written before is
//   not applied, which fits the bench note that the first writes may not stick. DDR comes
//   last (gpio_hold wrote it before OUT): today the pin is an output already, so this
//   changes nothing, but a pin that is still an input is then never driven before its
//   level is set. All three writes are repeated until the readback agrees, up to
//   WRITES_PER_ROUND times.
// - On the bench a level written very early was seen undone shortly afterwards (cause not
//   found). So the pin is read again after a settle time with no write in between, and
//   only that second reading counts as verified. If it disagrees the writes start again,
//   up to MAX_ROUNDS times.
//
// Time limit: a UHD call into a hung USB device may never return and cannot be cancelled.
// A second thread therefore ends the whole process when the time is up. If the stuck call
// is inside the kernel and cannot be interrupted, the process outlives even that (and
// SIGKILL), which is why the amp_safe wrapper stops waiting for it.
// The process ends with _exit as soon as the result line is out: the SDR keeps the pin
// state without being closed, and closing it could hang.
#include "gpio_port.hpp"

#include <poll.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <memory>
#include <string>
#include <thread>

// Exit codes (also in README.md and in amp_safe). Only 0 means the amplifier is safe.
enum {
  EXIT_SAFE = 0,            // pin verified in the amplifier-shutdown state
  EXIT_NOT_FOUND = 2,       // no SDR on USB; SDR not initialised by this run
  EXIT_ASSUME_ENABLED = 3,  // initialisation started, shutdown state not verified
  EXIT_TIMED_OUT = 4,       // time limit before the initialisation started
  EXIT_BUSY = 6,            // SDR on USB but not usable (in use, unresponsive); not initialised
  EXIT_AMBIGUOUS = 7,       // more than one SDR matches; none initialised
  EXIT_BAD_USAGE = 64,      // bad command line; SDR not touched
};

static const char* const DEFAULT_ARGS = "";   // as on the bench: the only SDR connected
static const double DEFAULT_TIMEOUT_S = 20.0;

static const int WRITES_PER_ROUND = 30;  // 30 x (50 + 50 ms) = 3 s
static const int MAX_ROUNDS = 3;
static const int READ_DELAY_MS = 50;     // from a write to its readback
static const int RETRY_DELAY_MS = 50;
#ifndef SDR_GPIO_SAFE_SETTLE_MS          // only the test build sets it, to run faster
#define SDR_GPIO_SAFE_SETTLE_MS 1500     // from the first good readback to the one that counts
#endif

static const std::string PIN = std::string(AMP_BANK) + " GPIO_0";
static const std::string NOT_INITIALISED =
    "SDR not initialised by this run, pin not changed by it; amplifier state unknown";
static const std::string ASSUME_ENABLED =
    "this run initialised the SDR, which drives the pin low, and did not verify it high again: "
    "assume the amplifier is ENABLED";

typedef std::chrono::steady_clock Clock;
static const Clock::time_point g_start = Clock::now();
static std::atomic<const char*> g_stage{"starting"};
static std::atomic<bool> g_initialising{false};  // true from just before the initialisation
static std::atomic<int> g_exit_code{-1};         // -1 until a thread has taken the result

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

// Print the one result line and end the process at once, without closing the SDR.
// Whichever thread gets here first decides the result; the other only makes sure that the
// process ends. Nothing here can block for long: the line is written with write(2) and
// only while standard output accepts it.
[[noreturn]] static void finish(int code, const std::string& text) {
  int taken = -1;
  if (g_exit_code.compare_exchange_strong(taken, code)) {
    const std::string line = "sdr_gpio_safe: " + text + "\n";
    size_t done = 0;
    while (done < line.size()) {
      struct pollfd out = {STDOUT_FILENO, POLLOUT, 0};
      if (::poll(&out, 1, 500) <= 0) break;
      const ssize_t n = ::write(STDOUT_FILENO, line.data() + done, line.size() - done);
      if (n <= 0) break;
      done += static_cast<size_t>(n);
    }
    _exit(code);
  }
  sleep_ms(700);  // the other thread is printing its line and exits first
  _exit(taken);
}

static void time_limit(double timeout_s) {
  std::this_thread::sleep_until(g_start + std::chrono::duration_cast<Clock::duration>(
                                              std::chrono::duration<double>(timeout_s)));
  const std::string what = "time limit of " + fmt("%.1f", timeout_s) + " s reached while " + g_stage.load() + "; ";
  if (g_initialising) finish(EXIT_ASSUME_ENABLED, "ASSUME_ENABLED: " + what + ASSUME_ENABLED);
  finish(EXIT_TIMED_OUT, "TIMEOUT: " + what + NOT_INITIALISED);
}

static bool reads_high(GpioPort& port) { return (port.readback() & AMP_PIN_MASK) != 0; }

// Find and initialise the SDR, then write and verify the pin. Ends the process.
[[noreturn]] static void make_safe(const std::string& args) {
  const std::string for_args = " for device arguments \"" + args + "\"";

  g_stage = "looking for the SDR";
  std::string sdr, error;
  int found = 0;
  try {
    found = find_sdr(args, sdr);
  } catch (const std::exception& e) {
    error = "; the search failed: " + one_line(e.what());
  } catch (...) {
    error = "; the search failed";
  }
  if (found == 0) {
    std::string where;
    bool on_usb = false;
    try {
      on_usb = sdr_on_usb(where);
    } catch (...) {
    }
    if (on_usb)
      finish(EXIT_BUSY, "BUSY: an SDR is connected (" + where + ") but UHD found none it can use" + for_args + error
                            + ": in use by another process (a running radar?), not responding, or not matching"
                              " the arguments; " + NOT_INITIALISED);
    finish(EXIT_NOT_FOUND, "NOT_FOUND: no SDR" + for_args + " and none on USB (unplugged or unpowered)" + error
                               + "; " + NOT_INITIALISED);
  }
  if (found > 1)
    finish(EXIT_AMBIGUOUS, "AMBIGUOUS: " + std::to_string(found) + " SDRs match" + for_args
                               + "; select one with --args; " + NOT_INITIALISED);

  g_initialising = true;
  g_stage = "initialising the SDR";
  const double init_start = elapsed_s();
  std::unique_ptr<GpioPort> port;
  try {
    port = open_sdr(args);
  } catch (const std::exception& e) {
    error = one_line(e.what());
  } catch (...) {
    error = "unknown error";
  }
  if (!port) finish(EXIT_ASSUME_ENABLED, "ASSUME_ENABLED: initialisation of " + sdr + " failed: " + error + "; " + ASSUME_ENABLED);
  const double init_s = elapsed_s() - init_start;

  int writes = 0;
  int rounds = 0;
  std::string detail = PIN + " never read high";
  error.clear();
  while (rounds < MAX_ROUNDS) {
    rounds++;
    g_stage = "writing the pin";
    bool ok = false;
    for (int i = 0; i < WRITES_PER_ROUND && !ok; i++) {
      if (i > 0) sleep_ms(RETRY_DELAY_MS);
      try {
        writes++;
        port->write("CTRL", 0, AMP_PIN_MASK);             // manual control, not ATR
        port->write("OUT", AMP_PIN_MASK, AMP_PIN_MASK);   // high = amplifier shutdown; after CTRL
        port->write("DDR", AMP_PIN_MASK, AMP_PIN_MASK);   // output; after its level is set
        sleep_ms(READ_DELAY_MS);
        ok = reads_high(*port);
      } catch (const std::exception& e) {
        error = one_line(e.what());
      } catch (...) {
        error = "unknown error";
      }
    }
    if (!ok) break;
    detail = PIN + " read high once but could not be read again";

    g_stage = "waiting to read the pin again";
    sleep_ms(SDR_GPIO_SAFE_SETTLE_MS);
    try {
      if (reads_high(*port))
        finish(EXIT_SAFE, "OK: " + PIN + " is a manual output commanded high (amplifier shutdown), read back high twice "
                              + std::to_string(SDR_GPIO_SAFE_SETTLE_MS) + " ms apart; " + std::to_string(writes)
                              + (writes == 1 ? " write, " : " writes, ") + "SDR initialised in " + fmt("%.2f", init_s)
                              + " s, total " + fmt("%.2f", elapsed_s()) + " s; " + sdr);
      detail = PIN + " went back to low after reading high";
    } catch (const std::exception& e) {
      error = one_line(e.what());
    } catch (...) {
      error = "unknown error";
    }
  }
  finish(EXIT_ASSUME_ENABLED, "ASSUME_ENABLED: " + detail + " (" + std::to_string(writes)
                                  + (writes == 1 ? " write, " : " writes, ") + std::to_string(rounds)
                                  + (rounds == 1 ? " round" : " rounds") + ")"
                                  + (error.empty() ? "" : "; last error: " + error) + "; " + ASSUME_ENABLED + "; " + sdr);
}

static void print_help() {
  std::printf(
      "Usage: sdr_gpio_safe [--args STRING] [--timeout SECONDS]\n"
      "\n"
      "Initialises the SDR, drives the amplifier-enable pin (%s GPIO_0) high, the level\n"
      "that shuts the amplifier down, as a manually controlled output, and verifies it\n"
      "by reading it back. It has no other action. Initialising the SDR itself drives\n"
      "the pin low (amplifier enabled) until the pin is written: see README.md.\n"
      "\n"
      "  --args STRING      UHD device arguments (default \"%s\")\n"
      "  --timeout SECONDS  give up after this long, 1-300 (default %.0f)\n"
      "  -h, --help         show this text; the SDR is not touched\n"
      "\n"
      "Prints one line to standard output; UHD's own messages go to standard error.\n"
      "Exit codes (only 0 means the amplifier is safe):\n"
      "   %d  OK              pin verified high (amplifier shutdown)\n"
      "   %d  NOT_FOUND       no SDR on USB; SDR not initialised, pin not changed\n"
      "   %d  ASSUME_ENABLED  SDR initialised but pin not verified high: assume that\n"
      "                      this run left the amplifier enabled\n"
      "   %d  TIMEOUT         time limit before the SDR was initialised; pin not changed\n"
      "   %d  BUSY            SDR on USB but in use or not responding; pin not changed\n"
      "   %d  AMBIGUOUS       more than one SDR matches; pin not changed\n"
      "  %d  USAGE           bad command line; SDR not touched\n",
      AMP_BANK, DEFAULT_ARGS, DEFAULT_TIMEOUT_S, EXIT_SAFE, EXIT_NOT_FOUND, EXIT_ASSUME_ENABLED, EXIT_TIMED_OUT,
      EXIT_BUSY, EXIT_AMBIGUOUS, EXIT_BAD_USAGE);
}

static bool parse_seconds(const char* text, double low, double high, double& value) {
  char* end = nullptr;
  value = std::strtod(text, &end);
  if (end == text || *end != '\0' || !std::isfinite(value)) return false;
  if (std::strspn(text, "0123456789.") != std::strlen(text)) return false;  // plain decimal only
  return value >= low && value <= high;
}

int main(int argc, char* argv[]) {
  // Everything on the command line is checked here, before anything can touch the SDR.
  std::string args = DEFAULT_ARGS;
  double timeout_s = DEFAULT_TIMEOUT_S;
  for (int i = 1; i < argc; i++)
    if (!std::strcmp(argv[i], "-h") || !std::strcmp(argv[i], "--help")) {
      print_help();
      return EXIT_SAFE;
    }
  for (int i = 1; i < argc; i += 2) {
    const std::string name = argv[i];
    const std::string untouched = " (see --help); SDR not touched";
    if (name != "--args" && name != "--timeout") finish(EXIT_BAD_USAGE, "USAGE: unknown argument " + name + untouched);
    if (i + 1 >= argc) finish(EXIT_BAD_USAGE, "USAGE: " + name + " needs a value" + untouched);
    if (name == "--args")
      args = argv[i + 1];
    else if (!parse_seconds(argv[i + 1], 1, 300, timeout_s))
      finish(EXIT_BAD_USAGE, "USAGE: bad value for --timeout: \"" + std::string(argv[i + 1]) + "\"" + untouched);
  }

  try {
    std::thread(time_limit, timeout_s).detach();
  } catch (const std::exception&) {
    finish(EXIT_TIMED_OUT, "TIMEOUT: cannot start the time-limit thread; " + NOT_INITIALISED);
  }
  make_safe(args);
}
