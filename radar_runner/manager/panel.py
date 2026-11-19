"""Front panel: the push button (GPIO 4) and the READY and RUNNING LEDs (GPIO 18, GPIO 17).

The button
    Wired active low to ground, with the Pi's internal pull-up, on long wires
    in a noisy box. Two layers:
      1. the kernel debounces the line (`kernel_debounce_s`): an edge is
         reported only once the level has held that long;
      2. `ButtonFilter` decides what a press is, from the edges that remain.
    A press is: the button held down for at least `press_min_s` and at most
    `press_max_s`, and released at least `press_gap_s` after the release of
    the previous press. It counts at the release. Everything else is ignored
    and counted: a glitch (too short), a held or stuck button (too long, and
    it does not repeat), a second press right after one (too soon), a release
    with no press before it (the button was already down when the manager
    started).

The LEDs
    Patterns by name (logic.LEDS): "off", "on", "fast_blink", "breathing".
    "breathing" is a slow fade through PWM on GPIO 18 if `breathing` is "pwm";
    with "blink" it is a slow blink, for the case that PWM does not work
    through the LED's built-in regulator.

gpiozero with the lgpio backend; the tests use gpiozero's mock pins.
"""
import logging
import threading
import time

log = logging.getLogger("manager.panel")

OFF, ON, FAST, BREATHE, SLOW = "off", "on", "fast_blink", "breathing", "slow_blink"


class ButtonFilter:
    """Turns debounced edges into presses. Pure: the caller passes the time of each edge."""

    def __init__(self, press_min_s=0.1, press_max_s=5.0, press_gap_s=1.0):
        self.press_min_s, self.press_max_s, self.press_gap_s = press_min_s, press_max_s, press_gap_s
        self.down_at = None
        self.last_press = None
        self.presses = 0
        self.ignored = {"too_short": 0, "too_long": 0, "too_soon": 0, "no_press_before_release": 0}

    def edge(self, t, pressed):
        """Feed one edge. Returns True if this edge completes a press."""
        if pressed:
            self.down_at = t
            return False
        if self.down_at is None:
            self.ignored["no_press_before_release"] += 1
            return False
        held, self.down_at = t - self.down_at, None
        if held < self.press_min_s:
            self.ignored["too_short"] += 1
        elif held > self.press_max_s:
            self.ignored["too_long"] += 1
        elif self.last_press is not None and t - self.last_press < self.press_gap_s:
            self.ignored["too_soon"] += 1
        else:
            self.last_press = t
            self.presses += 1
            return True
        return False

    def snapshot(self):
        return {"presses": self.presses, "ignored": dict(self.ignored), "down": self.down_at is not None}


class Panel:
    """The button and the two LEDs. Never raises after `open`; errors are kept in `error`."""

    def __init__(self, cfg, on_press, *, monotonic=time.monotonic, pin_factory=None):
        self.cfg = cfg
        self.on_press = on_press
        self._monotonic = monotonic
        self._pin_factory = pin_factory
        self.filter = ButtonFilter(cfg["press_min_s"], cfg["press_max_s"], cfg["press_gap_s"])
        self.error = ""
        self.shown = (None, None)
        self._lock = threading.Lock()
        self.button = self.yellow = self.green = None

    def open(self):
        """Claim the pins. Returns "" or the error text (the manager runs on without a panel)."""
        try:
            import gpiozero
            if self._pin_factory is None:
                from gpiozero.pins.lgpio import LGPIOFactory
                self._pin_factory = LGPIOFactory()
            cfg, factory = self.cfg, self._pin_factory
            self.pwm = cfg["breathing"] == "pwm"
            self.green = gpiozero.LED(cfg["green_gpio"], pin_factory=factory)
            if self.pwm:
                self.yellow = gpiozero.PWMLED(cfg["yellow_gpio"], frequency=cfg["pwm_hz"], pin_factory=factory)
            else:
                self.yellow = gpiozero.LED(cfg["yellow_gpio"], pin_factory=factory)
            self.button = gpiozero.Button(cfg["button_gpio"], pull_up=True,
                                          bounce_time=cfg["kernel_debounce_s"] or None, pin_factory=factory)
            self.button.when_pressed = lambda: self._edge(True)
            self.button.when_released = lambda: self._edge(False)
            self.error = ""
        except Exception as e:
            self.close()
            self.error = f"{type(e).__name__}: {e}"
        return self.error

    def _edge(self, pressed):
        with self._lock:
            accepted = self.filter.edge(self._monotonic(), pressed)
        if accepted:
            try:
                self.on_press()
            except Exception:
                log.exception("error in the button handler")

    def show(self, yellow, green):
        """Set both LEDs to a pattern. Does nothing if they already show it."""
        if (yellow, green) == self.shown or self.yellow is None:
            return
        cfg = self.cfg
        try:
            if green == ON:
                self.green.on()
            else:
                self.green.off()
            if yellow == BREATHE and not self.pwm:
                yellow_pattern = SLOW
            else:
                yellow_pattern = yellow
            if yellow_pattern == ON:
                self.yellow.on()
            elif yellow_pattern == FAST:
                self.yellow.blink(cfg["fast_blink_s"], cfg["fast_blink_s"])
            elif yellow_pattern == SLOW:
                self.yellow.blink(cfg["slow_blink_s"], cfg["slow_blink_s"])
            elif yellow_pattern == BREATHE:
                self.yellow.pulse(cfg["breath_s"], cfg["breath_s"])
            else:
                self.yellow.off()
            self.shown = (yellow, green)
            self.error = ""
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"

    def close(self):
        """Release the pins; the LEDs go off."""
        for device in (self.button, self.yellow, self.green):
            try:
                if device is not None:
                    device.close()
            except Exception:
                pass
        self.button = self.yellow = self.green = None
        self.shown = (None, None)

    def snapshot(self):
        out = {"available": self.yellow is not None, "error": self.error, "button": self.filter.snapshot(),
               "breathing": self.cfg["breathing"]}
        try:
            if self.button is not None:
                out["button"]["down_now"] = bool(self.button.is_pressed)
        except Exception:
            pass
        return out


class NoPanel:
    """Stands in when the panel is disabled."""
    error = ""
    shown = (None, None)

    def open(self):
        return ""

    def show(self, yellow, green):
        self.shown = (yellow, green)

    def close(self):
        pass

    def snapshot(self):
        return {"available": False, "error": "disabled (manager.panel.enabled)"}
