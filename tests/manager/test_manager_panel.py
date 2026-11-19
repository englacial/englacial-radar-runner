"""Front panel (radar_runner/manager/panel.py): button debounce and LED patterns, with gpiozero's mock pins.

No real pin is touched: every Panel here gets a MockFactory.
"""
import time

import pytest
from gpiozero.pins.mock import MockFactory, MockPin, MockPWMPin

from radar_runner.common import config as _shared_config

MANAGER_DEFAULTS = _shared_config.DEFAULTS["manager"]
from radar_runner.manager.panel import ButtonFilter, NoPanel, Panel


# ------------------------------------------------------------ what a press is

def presses(edges, **kw):
    """Feed (time, pressed) edges; return the times at which a press was accepted."""
    f = ButtonFilter(**kw)
    return [t for t, pressed in edges if f.edge(t, pressed)], f


def test_a_press_is_down_for_long_enough_and_counts_at_the_release():
    accepted, f = presses([(10.0, True), (10.3, False)])
    assert accepted == [10.3] and f.presses == 1


def test_default_thresholds():
    panel = MANAGER_DEFAULTS["panel"]
    assert (panel["press_min_s"], panel["press_max_s"], panel["press_gap_s"]) == (0.1, 5.0, 1.0)
    assert panel["kernel_debounce_s"] == 0.02
    f = ButtonFilter()
    assert (f.press_min_s, f.press_max_s, f.press_gap_s) == (0.1, 5.0, 1.0)


@pytest.mark.parametrize("held, ok", [(0.001, False), (0.05, False), (0.099, False), (0.1, True), (1.0, True),
                                      (5.0, True), (5.01, False), (3600.0, False)])
def test_press_length_window(held, ok):
    accepted, f = presses([(0.0, True), (held, False)])
    assert bool(accepted) is ok
    if not ok:
        assert f.ignored["too_short" if held < 0.1 else "too_long"] == 1


def test_spurious_edges_are_harmless():
    # A burst of noise: 200 short pulses in two seconds.
    edges = []
    for i in range(200):
        edges += [(50.0 + i * 0.01, True), (50.0 + i * 0.01 + 0.004, False)]
    accepted, f = presses(edges)
    assert accepted == [] and f.ignored["too_short"] == 200 and f.presses == 0


def test_bounce_around_a_real_press_gives_one_press():
    edges = [(1.000, True), (1.002, False), (1.004, True), (1.006, False),      # bounce on the way down
             (1.010, True), (1.400, False),                                     # the press itself
             (1.402, True), (1.405, False), (1.409, True), (1.411, False)]      # bounce on the way up
    accepted, f = presses(edges)
    assert accepted == [1.400] and f.ignored["too_short"] == 4


def test_a_glitch_in_the_middle_of_a_press_does_not_make_two():
    edges = [(1.0, True), (1.3, False), (1.301, True), (1.6, False)]
    accepted, f = presses(edges)
    assert accepted == [1.3] and f.ignored["too_soon"] == 1


def test_a_second_press_needs_a_gap():
    accepted, f = presses([(1.0, True), (1.2, False), (1.5, True), (1.9, False), (2.3, True), (2.6, False)])
    assert accepted == [1.2, 2.6] and f.ignored["too_soon"] == 1               # 1.9 is 0.7 s after 1.2


def test_a_held_or_stuck_button_does_nothing_and_does_not_repeat():
    f = ButtonFilter()
    assert not f.edge(0.0, True)
    assert f.snapshot()["down"] is True
    assert not f.edge(86400.0, False)                       # released a day later
    assert f.presses == 0 and f.ignored["too_long"] == 1
    assert f.edge(86401.0, True) is False and f.edge(86401.3, False) is True    # and it works again


def test_a_release_without_a_press_is_ignored():
    # The button was already down when the manager started, or an edge was lost.
    accepted, f = presses([(5.0, False), (6.0, True), (6.2, False)])
    assert accepted == [6.2] and f.ignored["no_press_before_release"] == 1


def test_two_press_edges_in_a_row_time_from_the_later_one():
    accepted, _ = presses([(1.0, True), (1.95, True), (2.0, False)])
    assert accepted == []                                   # only 50 ms since the last known press edge


# --------------------------------------------------------------- with mock pins

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(breathing="pwm", **over):
    factory = MockFactory(pin_class=MockPWMPin if breathing == "pwm" else MockPin)
    cfg = dict(MANAGER_DEFAULTS["panel"], breathing=breathing, fast_blink_s=0.02, slow_blink_s=0.06,
               **{"breath_s": 0.4, **over})
    clock = Clock()
    pressed = []
    panel = Panel(cfg, lambda: pressed.append(clock.t), monotonic=clock, pin_factory=factory)
    assert panel.open() == ""
    return panel, factory, clock, pressed


def push(factory, clock, held, pin=4):
    factory.pin(pin).drive_low()
    clock.t += held
    factory.pin(pin).drive_high()


def test_pins_are_claimed_as_the_hardware_is_wired():
    panel, factory, _, _ = make()
    button, green, yellow = factory.pin(4), factory.pin(17), factory.pin(18)
    assert button.function == "input" and button.pull == "up"       # active low, internal pull-up
    assert green.function == "output" and yellow.function == "output"
    assert button.bounce == 0.02                                    # the kernel's debounce is asked for
    assert panel.button.is_pressed is False
    assert yellow.frequency == 200                                  # PWM on the READY LED
    panel.close()


def test_button_press_through_the_pins():
    panel, factory, clock, pressed = make()
    push(factory, clock, 0.3)
    assert pressed == [1000.3]
    clock.t += 5
    push(factory, clock, 0.01)                              # glitch
    clock.t += 5
    push(factory, clock, 30.0)                              # leaned on
    assert pressed == [1000.3]
    snap = panel.snapshot()
    assert snap["available"] and snap["button"]["presses"] == 1
    assert snap["button"]["ignored"]["too_short"] == 1 and snap["button"]["ignored"]["too_long"] == 1
    assert snap["button"]["down_now"] is False
    panel.close()


def test_button_already_down_at_start_is_ignored_until_released():
    factory = MockFactory(pin_class=MockPWMPin)
    factory.pin(4)                                          # create it ...
    clock = Clock()
    pressed = []
    panel = Panel(dict(MANAGER_DEFAULTS["panel"]), lambda: pressed.append(1), monotonic=clock, pin_factory=factory)
    assert panel.open() == ""
    factory.pin(4).drive_low()                              # ... held down from before the first edge is seen
    panel.filter.down_at = None                             # as if the press edge came before open()
    clock.t += 0.5
    factory.pin(4).drive_high()
    assert pressed == [] and panel.filter.ignored["no_press_before_release"] == 1
    clock.t += 2
    push(factory, clock, 0.2)
    assert pressed == [1]
    panel.close()


def test_an_error_in_the_press_handler_does_not_break_the_button(caplog):
    factory = MockFactory(pin_class=MockPWMPin)
    clock = Clock()
    calls = []

    def handler():
        calls.append(1)
        raise RuntimeError("boom")
    panel = Panel(dict(MANAGER_DEFAULTS["panel"]), handler, monotonic=clock, pin_factory=factory)
    panel.open()
    push(factory, clock, 0.2)
    clock.t += 2
    push(factory, clock, 0.2)
    assert calls == [1, 1] and "error in the button handler" in caplog.text
    panel.close()


def levels(pin, seconds):
    """The distinct values a pin takes over `seconds`."""
    pin.clear_states()
    time.sleep(seconds)
    return [s.state for s in pin.states]


def test_led_patterns_of_the_state_table_with_pwm():
    panel, factory, _, _ = make("pwm")
    yellow, green = factory.pin(18), factory.pin(17)
    panel.show("on", "off")                                 # ready
    assert yellow.state == 1.0 and green.state == 0
    panel.show("off", "on")                                 # running
    assert yellow.state == 0.0 and green.state == 1
    panel.show("fast_blink", "off")                         # not ready, fault
    seen = levels(yellow, 0.3)
    assert set(seen) == {0.0, 1.0} and len(seen) >= 8 and green.state == 0
    panel.show("breathing", "off")                          # starting, stopping
    seen = levels(yellow, 1.0)
    assert any(0.0 < v < 1.0 for v in seen) and len(set(seen)) > 5       # a fade, not a blink
    panel.show("off", "off")                                # power fail
    time.sleep(0.05)
    assert yellow.state == 0.0 and green.state == 0 and levels(yellow, 0.1) == [0.0]
    panel.close()


def test_breathing_falls_back_to_a_slow_blink_without_pwm():
    panel, factory, _, _ = make("blink")
    yellow = factory.pin(18)
    assert type(yellow) is MockPin                          # a plain on/off pin is enough
    panel.show("breathing", "off")
    slow = levels(yellow, 0.5)
    assert set(slow) <= {0, 1, True, False} and 5 <= len(slow) <= 12        # 0.06 s on, 0.06 s off
    panel.show("fast_blink", "off")
    fast = levels(yellow, 0.5)
    assert len(fast) > 2 * len(slow) - 4                    # clearly faster than the slow blink
    panel.show("on", "off")
    assert yellow.state == 1
    panel.close()


def test_show_does_nothing_when_the_pattern_is_unchanged():
    panel, factory, _, _ = make()
    panel.show("fast_blink", "off")
    thread = panel.yellow._blink_thread
    panel.show("fast_blink", "off")
    assert panel.yellow._blink_thread is thread             # the blink was not restarted
    panel.close()


def test_close_releases_the_pins_and_switches_the_leds_off():
    panel, factory, _, _ = make()
    panel.show("on", "on")
    panel.close()
    assert factory.pin(18).state == 0 and factory.pin(17).state == 0
    assert panel.snapshot()["available"] is False and panel.shown == (None, None)
    panel.show("on", "on")                                  # harmless after close
    panel.close()


def test_panel_that_cannot_be_opened_reports_it_and_the_rest_keeps_working():
    class BrokenFactory:
        def pin(self, *a, **k):
            raise OSError("can not open gpiochip")

        def reserve_pins(self, *a, **k):
            raise OSError("can not open gpiochip")

        def release_pins(self, *a, **k):
            pass

        def close(self):
            pass
    panel = Panel(dict(MANAGER_DEFAULTS["panel"]), lambda: None, pin_factory=BrokenFactory())
    error = panel.open()
    assert "gpiochip" in error and panel.snapshot() == {
        "available": False, "error": error, "breathing": "pwm",
        "button": {"presses": 0, "ignored": {"too_short": 0, "too_long": 0, "too_soon": 0,
                                             "no_press_before_release": 0}, "down": False}}
    panel.show("on", "off")                                 # no exception
    panel.close()


def test_no_panel_stand_in():
    panel = NoPanel()
    assert panel.open() == ""
    panel.show("on", "off")
    assert panel.shown == ("on", "off") and panel.snapshot()["available"] is False
    panel.close()
