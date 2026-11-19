"""Live test of the front panel: cycles the LED patterns and reports the button.

    cd ~/englacial-radar-runner
    .venv/bin/python -B -m radar_runner.manager.panel_test              # about 50 s
    .venv/bin/python -B -m radar_runner.manager.panel_test --seconds 10 --breathing blink

Touches only GPIO 4 (button, input with pull-up), GPIO 17 (green) and GPIO 18
(yellow). It refuses to run while another program holds one of them (the radar
manager service, for one: stop it first). Uses the same code and settings as
the manager (panel.py, manager.panel in the config).

What to look at (the Pi cannot see its own LEDs):

  1. Each step prints the state and what the two LEDs should show. Does every
     step match? (The table is in the plan: "States, status LEDs and
     front-panel switch".)
  2. "breathing (PWM)": does the yellow LED fade smoothly up and down, about
     2.4 s per cycle, without steps, flicker, or staying fully on or off? If
     not, PWM does not get through the LED's built-in regulator: set
     `manager.panel.breathing: blink` in /etc/radar-runner/local.yaml.
  3. "breathing (slow blink)": is it clearly slower than the fast blink of
     "not ready", so that the two cannot be confused at a glance?
  4. Button: press it normally a few times, at least a second apart: each press
     should print "PRESS accepted" at the release. Hold it for more than 5 s:
     "ignored (too_long)". Tap it as briefly as you can: accepted, or "ignored
     (too_short)" for taps under 0.1 s. With nobody touching it, nothing at all
     should be printed about the button: any line then is noise on the wire.
"""
import argparse
import sys
import time

from radar_runner.common import config as common_config

from . import logic as L
from .panel import Panel

STEPS = (
    (L.NOT_READY, "yellow FAST BLINK, green off"),
    (L.READY, "yellow SOLID ON, green off"),
    (L.STARTING, "yellow BREATHING, green off"),
    (L.RUNNING, "yellow off, green SOLID ON"),
    (L.STOPPING, "yellow BREATHING, green off"),
    (L.FAULT, "yellow FAST BLINK, green off"),
    (L.POWER_FAIL, "yellow off, green off"),
)


def holders(pins):
    """Return {pin: consumer} for the pins that another program holds."""
    import lgpio
    held = {}
    for chip in range(8):
        try:
            handle = lgpio.gpiochip_open(chip)
        except Exception:
            continue
        try:
            if "pinctrl-rp1" not in [str(x) for x in lgpio.gpio_get_chip_info(handle)]:
                continue                        # the Pi 5 header is the chip labelled pinctrl-rp1
            for pin in pins:
                info = lgpio.gpio_get_line_info(handle, pin)
                if info[2] & 1:                 # the kernel reports the line as in use
                    held[pin] = info[4] or "another program"
        finally:
            lgpio.gpiochip_close(handle)
    return held


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seconds", type=float, default=5.0, help="time per step (default: %(default)s)")
    parser.add_argument("--breathing", choices=["pwm", "blink", "both"], default="both",
                        help="which form of breathing to show (default: both, one after the other)")
    args = parser.parse_args(argv)

    config, _ = common_config.load()
    cfg = dict(config["manager"]["panel"])
    pins = (cfg["button_gpio"], cfg["green_gpio"], cfg["yellow_gpio"])
    held = holders(pins)
    if held:
        print("in use, not touching anything: " + ", ".join(f"GPIO {p} by {who}" for p, who in held.items()))
        return 1

    t0 = time.monotonic()

    def stamp():
        return f"{time.monotonic() - t0:7.2f} s"

    result = 0
    for mode in (("pwm", "blink") if args.breathing == "both" else (args.breathing,)):
        cfg["breathing"] = mode
        panel = Panel(cfg, lambda: print(f"{stamp()}  button: PRESS accepted", flush=True))
        error = panel.open()
        if error:
            print(f"cannot open the panel pins: {error}")
            return 1
        real_edge = panel.filter.edge

        def edge(t, pressed, real_edge=real_edge, panel=panel):
            before = dict(panel.filter.ignored)
            accepted = real_edge(t, pressed)
            why = [k for k, v in panel.filter.ignored.items() if v != before[k]]
            print(f"{stamp()}  button: {'down' if pressed else 'up'}"
                  + (f"  -> ignored ({why[0]})" if why else ""), flush=True)
            return accepted
        panel.filter.edge = edge
        print(f"--- breathing shown as: {'PWM fade' if mode == 'pwm' else 'slow blink'}; "
              f"button down now: {panel.snapshot()['button'].get('down_now')}", flush=True)
        try:
            for state, text in STEPS:
                if mode == "blink" and args.breathing == "both" and state not in (L.NOT_READY, L.STARTING):
                    continue                    # second pass: only the two blinks, to compare them
                yellow, green = L.LEDS[state]
                shown = text.replace("BREATHING", "BREATHING (PWM fade)" if mode == "pwm"
                                     else "BREATHING (slow blink)")
                print(f"{stamp()}  state {state:10s} {shown}", flush=True)
                panel.show(yellow, green)
                if panel.error:
                    print(f"           LED error: {panel.error}", flush=True)
                    result = 1
                time.sleep(args.seconds)
        except KeyboardInterrupt:
            result = 130
        finally:
            snap = panel.snapshot()["button"]
            panel.close()
        print(f"{stamp()}  LEDs off. Button: {snap['presses']} presses accepted, ignored: {snap['ignored']}",
              flush=True)
        if result:
            break
    return result


if __name__ == "__main__":
    sys.exit(main())
