idf_component_register(
    SRCS "luka_ww.cc"
    INCLUDE_DIRS "include"
    REQUIRES esp-tflite-micro esp-micro-speech-features
)

# TFLite Micro es C++17 y arrastra plantillas pesadas; sin esto el componente
# hereda el estándar del proyecto y no compila.
target_compile_options(${COMPONENT_LIB} PRIVATE -std=gnu++17 -fno-exceptions -fno-rtti)
