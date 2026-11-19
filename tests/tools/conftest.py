"""Shared pieces of the tests of tools/sdr_gpio_safe.

No test here can touch the SDR. The program's logic is tested in a build that has
a stand-in device instead of UHD (fake_port.cpp); the real binary is only ever
started with command lines that the stand-in build has just rejected or answered
with its help text, and only if it was built from the sources as they are now.
"""
import shutil
import subprocess

import pytest

from tool_helpers import REAL_SOURCES, REAL_TOOL, TOOL_DIR


def build_fake(directory, *flags):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("no g++")
    binary = directory / "sdr_gpio_safe_fake"
    subprocess.run([compiler, "-std=c++17", "-O1", "-pthread", *flags,
                    str(TOOL_DIR / "sdr_gpio_safe.cpp"), str(TOOL_DIR / "fake_port.cpp"), "-o", str(binary)],
                   check=True, timeout=300)
    libraries = subprocess.run(["ldd", str(binary)], capture_output=True, text=True).stdout
    assert "uhd" not in libraries and "usb" not in libraries
    return binary


@pytest.fixture(scope="session")
def fake_tool(tmp_path_factory):
    """sdr_gpio_safe compiled with the stand-in device (no UHD in it) and a short settle time."""
    return build_fake(tmp_path_factory.mktemp("fast"), "-DSDR_GPIO_SAFE_SETTLE_MS=60")


@pytest.fixture(scope="session")
def fake_tool_as_built(tmp_path_factory):
    """The same with the settle time of the real build."""
    return build_fake(tmp_path_factory.mktemp("as_built"))


@pytest.fixture(scope="session")
def real_tool():
    """The real binary, if it is built and not older than its sources."""
    if not REAL_TOOL.exists():
        pytest.skip(f"{REAL_TOOL} is not built")
    built = REAL_TOOL.stat().st_mtime
    if any((TOOL_DIR / name).stat().st_mtime > built for name in REAL_SOURCES):
        pytest.skip(f"{REAL_TOOL} is older than its sources: rebuild it")
    return REAL_TOOL
