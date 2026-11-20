// Does the SDR see a PPS on its REF port? Reads the SDR's time at the last PPS edge for a few
// seconds, with the clock on the internal reference and the time source set to external.
// Nothing is transmitted and no front-panel pin is driven. Exit 0: PPS seen. 1: none. 2: error.
//   pps_check [seconds]      default 8
// Build (no conda environment needed):
//   U=$HOME/uhd-patched-gpio; C=$HOME/miniforge3/envs/orca
//   g++ -std=c++17 -O2 pps_check.cpp -o pps_check -isystem $U/include -isystem $C/include \
//       -L$U/lib -luhd -lpthread -Wl,-rpath,$U/lib -Wl,-rpath,$C/lib
// Run only while the radar is not running (the SDR can have one user at a time).
#include <uhd/usrp/multi_usrp.hpp>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <string>
#include <thread>

int main(int argc, char* argv[]) {
    const int seconds = argc > 1 ? std::stoi(argv[1]) : 8;
    try {
        auto usrp = uhd::usrp::multi_usrp::make(std::string("type=b200"));
        usrp->set_clock_source("internal");
        usrp->set_time_source("external");
        usrp->set_time_now(uhd::time_spec_t(0.0));
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        double last = usrp->get_time_last_pps().get_real_secs();
        int edges = 0;
        double worst = 0.0;
        std::printf("clock source %s, time source %s\n", usrp->get_clock_source(0).c_str(),
                    usrp->get_time_source(0).c_str());
        for (int i = 0; i < seconds * 10; i++) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            const double t = usrp->get_time_last_pps().get_real_secs();
            if (t != last) {
                const double step = t - last;
                std::printf("PPS edge at SDR time %.9f s (%.9f s after the one before)\n", t, step);
                if (edges > 0) worst = std::max(worst, std::fabs(step - 1.0));
                edges++;
                last = t;
            }
        }
        if (edges < 2) {
            std::printf("NO PPS: %d edge(s) in %d s\n", edges, seconds);
            return 1;
        }
        // Set the SDR's time at the next edge and check that it took.
        usrp->set_time_next_pps(uhd::time_spec_t(1000.0));
        std::this_thread::sleep_for(std::chrono::milliseconds(1500));
        const double set = usrp->get_time_last_pps().get_real_secs();
        std::printf("PPS SEEN: %d edges in %d s; spacing differs from 1 s by at most %.1f us "
                    "(against the SDR's internal clock)\n", edges, seconds, worst * 1e6);
        std::printf("time set at a PPS edge: last edge now reads %.9f s (1000 or 1001 expected)\n", set);
        return (set == 1000.0 || set == 1001.0) ? 0 : 2;
    } catch (const std::exception& e) {
        std::printf("ERROR: %s\n", e.what());
        return 2;
    }
}
