"""Paths and the command runner shared by the tests of tools/sdr_gpio_safe (see conftest.py)."""
import os
import subprocess
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[2] / "tools" / "sdr_gpio_safe"
AMP_SAFE = TOOL_DIR / "amp_safe"
REAL_TOOL = TOOL_DIR / "build" / "sdr_gpio_safe"
REAL_SOURCES = ("sdr_gpio_safe.cpp", "uhd_port.cpp", "gpio_port.hpp", "CMakeLists.txt")


def run(argv, fake=None, timeout=20):
    """Run a command; `fake` is the stand-in device's behaviour (see fake_port.cpp)."""
    env = {k: v for k, v in os.environ.items() if k != "SDR_GPIO_SAFE_FAKE"}
    if fake is not None:
        env["SDR_GPIO_SAFE_FAKE"] = fake
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL, timeout=timeout)
