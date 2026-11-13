cmake_minimum_required(VERSION 3.22)
# Build main and its declared/transitive dependencies only. The ranging
# application does not use esp_lcd; avoid compiling unrelated LCD drivers.
set(COMPONENTS main)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(qar_ftm_anchor)
