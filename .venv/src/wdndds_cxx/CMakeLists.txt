cmake_minimum_required(VERSION 3.16)
project(wdndds_string LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

set(FATDDS_WS "/tmp/fastdds_ws/install")
set(WDNDDS_CXX_DIR "${CMAKE_CURRENT_SOURCE_DIR}")
set(GEN_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated")

file(MAKE_DIRECTORY "${GEN_DIR}")

find_package(SWIG REQUIRED)
find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
include_directories(
    ${FATDDS_WS}/include
    ${WDNDDS_CXX_DIR}/include
    ${Python3_INCLUDE_DIRS}
)
link_directories(${FATDDS_WS}/lib)

# Step 1: SWIG generates the Python wrapper C file.
set(SWIG_WRAP_C "${GEN_DIR}/WdnddsStringPYTHON_wrap.c")
set(SWIG_WRAP_PY "${GEN_DIR}/wdndds_string.py")
add_custom_command(
    OUTPUT "${SWIG_WRAP_C}" "${SWIG_WRAP_PY}"
    COMMAND ${SWIG_EXECUTABLE} -python -c++ -outdir "${GEN_DIR}"
        -I${FATDDS_WS}/include
        -o "${SWIG_WRAP_C}"
        "${WDNDDS_CXX_DIR}/src/WdnddsString.i"
    DEPENDS "${WDNDDS_CXX_DIR}/src/WdnddsString.i"
            "${WDNDDS_CXX_DIR}/include/WdnddsString.hpp"
    COMMENT "Running SWIG on WdnddsString.i"
)

# Step 2: compile the SWIG-generated C++ glue to a static library.
# SWIG with -c++ emits .cxx in the .cxx/.cpp extension; force C++ even
# when the file is .c (older SWIG just appends .c).
set_source_files_properties("${SWIG_WRAP_C}" PROPERTIES LANGUAGE CXX)
add_library(wdndds_string_swig STATIC "${SWIG_WRAP_C}")
target_include_directories(wdndds_string_swig PUBLIC
    ${Python3_INCLUDE_DIRS}
)

# Step 3: C++ impl as a separate static lib.
add_library(wdndds_string_impl STATIC src/WdnddsString.cpp)
target_include_directories(wdndds_string_impl PUBLIC
    ${FATDDS_WS}/include
    ${WDNDDS_CXX_DIR}/include
)

# Step 4: final SHARED library combines impl + SWIG glue.
add_library(_wdndds_string SHARED
    $<TARGET_OBJECTS:wdndds_string_impl>
    $<TARGET_OBJECTS:wdndds_string_swig>
)
target_include_directories(_wdndds_string PUBLIC
    ${FATDDS_WS}/include
    ${WDNDDS_CXX_DIR}/include
    ${Python3_INCLUDE_DIRS}
)
target_link_libraries(_wdndds_string PRIVATE
    fastrtps fastcdr
    ${Python3_LIBRARIES}
)
set_target_properties(_wdndds_string PROPERTIES
    PREFIX ""
    LIBRARY_OUTPUT_DIRECTORY "${WDNDDS_CXX_DIR}/generated"
)

# Convenience: copy the SWIG-generated Python module alongside the .so.
add_custom_command(TARGET _wdndds_string POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${SWIG_WRAP_PY}"
        "${WDNDDS_CXX_DIR}/generated/wdndds_string.py"
    COMMENT "Copying SWIG-generated Python wrapper"
)