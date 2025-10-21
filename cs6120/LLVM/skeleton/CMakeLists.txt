add_llvm_pass_plugin(SkeletonPass
    # List your source files here.
   
    DCE.cpp
)
find_package(LLVM REQUIRED CONFIG)
include_directories(${LLVM_INCLUDE_DIRS})
add_definitions(${LLVM_DEFINITIONS})
