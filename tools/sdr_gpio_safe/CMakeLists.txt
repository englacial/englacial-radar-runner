# Build of sdr_gpio_safe against the patched UHD that ORCA uses. See README.md.
#   cmake -S tools/sdr_gpio_safe -B tools/sdr_gpio_safe/build && cmake --build tools/sdr_gpio_safe/build
# Configuring and building do not touch the SDR.
cmake_minimum_required(VERSION 3.16)
project(sdr_gpio_safe CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
add_compile_options(-Wall -Wextra -Wno-psabi)
find_package(Threads REQUIRED)

# Only this UHD is ever used: no search of the system or of any other prefix. It is the build
# that leaves the front-panel pins as inputs at initialisation (docs/uhd-proposals), the same
# one ORCA runs against (manager.orca.build_commands); setup/check.py compares the two.
set(UHD_PREFIX "$ENV{HOME}/uhd-patched-gpio" CACHE PATH "Install prefix of the patched UHD build")
unset(UHD_INCLUDE_DIR CACHE)   # found afresh each time, so that a changed UHD_PREFIX takes effect
unset(UHD_LIBRARY CACHE)
find_path(UHD_INCLUDE_DIR uhd/usrp/multi_usrp.hpp PATHS "${UHD_PREFIX}/include" NO_DEFAULT_PATH)
find_library(UHD_LIBRARY uhd PATHS "${UHD_PREFIX}/lib" NO_DEFAULT_PATH)
if(NOT UHD_INCLUDE_DIR OR NOT UHD_LIBRARY)
  message(FATAL_ERROR "No UHD in ${UHD_PREFIX}: set -DUHD_PREFIX=<install prefix of the patched UHD>")
endif()

# UHD's headers need the Boost headers it was built with, and libuhd needs that
# environment's libraries at run time. The UHD build records where they are.
set(_deps "$ENV{CONDA_PREFIX}")
set(_stamp "${UHD_PREFIX}/ORCA_UHD_BUILD.yaml")
if(EXISTS "${_stamp}")
  file(STRINGS "${_stamp}" _line REGEX "^conda_prefix:")
  string(REGEX REPLACE "^conda_prefix:[ \t]*" "" _deps "${_line}")
  file(STRINGS "${_stamp}" _commit REGEX "^commit:")
  message(STATUS "UHD build stamp: ${_commit}")
endif()
set(UHD_DEPS_PREFIX "${_deps}" CACHE PATH "Prefix of the environment the patched UHD was built in (Boost)")
if(NOT EXISTS "${UHD_DEPS_PREFIX}/include/boost/version.hpp")
  message(FATAL_ERROR "No Boost headers in '${UHD_DEPS_PREFIX}': set -DUHD_DEPS_PREFIX=<conda prefix of the UHD build>")
endif()
message(STATUS "UHD library: ${UHD_LIBRARY}")
message(STATUS "Boost headers and run-time libraries: ${UHD_DEPS_PREFIX}")

add_executable(sdr_gpio_safe sdr_gpio_safe.cpp uhd_port.cpp)
target_include_directories(sdr_gpio_safe SYSTEM PRIVATE "${UHD_INCLUDE_DIR}" "${UHD_DEPS_PREFIX}/include")
target_link_libraries(sdr_gpio_safe PRIVATE "${UHD_LIBRARY}" Threads::Threads)
set_target_properties(sdr_gpio_safe PROPERTIES
  BUILD_RPATH "${UHD_PREFIX}/lib;${UHD_DEPS_PREFIX}/lib"
  INSTALL_RPATH "${UHD_PREFIX}/lib;${UHD_DEPS_PREFIX}/lib")

# The same program on a stand-in SDR, for the tests. No UHD in it.
add_executable(sdr_gpio_safe_fake sdr_gpio_safe.cpp fake_port.cpp)
target_link_libraries(sdr_gpio_safe_fake PRIVATE Threads::Threads)
